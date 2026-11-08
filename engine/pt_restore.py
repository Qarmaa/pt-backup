#!/usr/bin/env python3
"""
pt_restore — распаковка сессий Pro Tools после pt_backup.

Папка на сетевом диске (SMB/AFP/NFS) не меняется: сначала она копируется на
локальный диск (rsync, в --to, по умолчанию /Volumes/Current projects/_temp/), и все
операции идут над копией. При повторном запуске докопируется только новое;
FLAC, для которых в копии уже есть WAV, повторно не копируются.
Папка на локальном диске распаковывается на месте (--copy — всё равно копировать).

Для каждой сессии:
  1. FLAC -> WAV (flac -d --delete-input-file) в "Audio Files" и "Bounced Files",
     а также все FLAC от бэкапа вне сессий (Source, PM, ME, …);
     WAV-чанки (BWF-таймкод и т.п.) восстанавливаются из FLAC
  0. файлы-ссылки на дубликаты (.ptref) собираются в исходные файлы; копии в Source,
     чей оригинал есть в сессии, остаются ссылками (собрать: --source-dups)
  2. "Session File Backups*.zip" распаковываются в "Session File Backups",
     архив удаляется после успешной распаковки

Примеры:
  pt_restore.py "/Volumes/Storage A/Sessions_2/MyFilm"       # -> /Volumes/Current projects/_temp/MyFilm
  pt_restore.py "/Volumes/Storage A/Sessions_2/MyFilm/Ep02"  # одна сессия
  pt_restore.py --to "/Volumes/Work SSD" "/Volumes/Storage A/Sessions_2/MyFilm"
  pt_restore.py -n -v "/Volumes/Storage A/Sessions_2/MyFilm"  # сухой прогон
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import hashlib
import json
import tarfile
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pt_backup import (_quiet_close, is_audio, AUDIO_DIR, audio_twin, is_session, session_media, EXTERNAL_VIDEO_DIR, ptwork_entry_for, record_run, RESTORED_FILE, read_restored, name_key, is_network, rsync_retry, SMB_HOST, SMB_PASS, SMB_USER, make_smb, run_quiet, flac_meta, PTBAK_EXT, PTBAK_MANIFEST, ZSTD, ptx_crypt, MIX_PREP, PTWORK_FILE, video_probe, DEDUP_INDEX, PTREF_EXT, read_stub, stub_target, wave_info, BACKUPS_DIR, CHUNKS_EXT, CHUNKS_HEAD, CHUNKS_MAGIC, FLAC_DIRS, PARTIAL_DIR, UI, default_log, find_rsync,
                       find_sessions, fmt_bytes, forget, kill_all, popen, run_main,
                       run_rsync)

RESTORE_TO = "/Volumes/Current projects/_temp/"  # куда копировать по умолчанию (--to)
COPY_RETRIES = 3

# -f — перезаписать WAV, оставшийся от прерванного запуска;
# --keep-foreign-metadata-if-present — вернуть чанки, сохранённые при сжатии
# с --keep-foreign-metadata (без него теряется bext с таймкодом BWF)
FLAC_FLAGS = ["-d", "--delete-input-file", "--keep-foreign-metadata-if-present", "-f"]

STEPS = ("flac", "backups")


# ----------------------------------------- ручная сборка WAV из блоков FLAC --

def rebuild_wav(flac: str, f: Path) -> str | None:
    """Собирает исходный WAV побайтово: сохранённые блоки + аудио из FLAC.

    Нужна, когда flac не может сам воспроизвести заголовок (например, PCM с
    нестандартным 40-байтным блоком fmt) и падает на проверке foreign metadata.
    Возвращает текст ошибки или None при успехе; FLAC удаляется только при успехе.
    """
    try:
        m = flac_meta(f)
        bps, chunks = m["bps"], m["riff"]
    except (OSError, ValueError) as e:
        return f"чтение FLAC: {e}"
    if not chunks or chunks[0][:4] not in (b"RIFF", b"RF64", b"BW64") or chunks[0][8:12] != b"WAVE":
        return "в FLAC нет сохранённого заголовка RIFF/WAVE"
    riff_size = struct.unpack("<I", chunks[0][4:8])[0]
    data64 = None
    if chunks[0][:4] != b"RIFF":  # RF64/BW64: настоящие размеры в ds64
        ds = next((c for c in chunks[1:] if c[:4] == b"ds64"), None)
        if ds is None:
            return "RF64 без блока ds64"
        riff_size, data64 = struct.unpack("<QQ", ds[8:24])

    wav = f.with_suffix(".wav")
    raw = f.with_name(f.name + ".raw.part")
    tmp = wav.with_name(wav.name + ".part")
    try:
        # аудио как в WAV: little-endian, 8 бит — беззнаковые, остальное — со знаком;
        # MD5 аудио flac проверяет сам
        r = subprocess.run([flac, "-d", "-f", "-s", "--force-raw-format", "--endian=little",
                            "--sign=" + ("unsigned" if bps <= 8 else "signed"),
                            "-o", str(raw), str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            return r.stderr.strip()[-500:]
        with open(tmp, "wb") as out:
            out.write(chunks[0][:12])
            for c in chunks[1:]:
                if c[:4] == b"data":
                    size = struct.unpack("<I", c[4:8])[0]
                    if size == 0xFFFFFFFF and data64 is not None:
                        size = data64
                    if raw.stat().st_size != size:
                        return f"размер аудио {raw.stat().st_size} ≠ {size} из заголовка"
                    out.write(c[:8])
                    with open(raw, "rb") as a:
                        shutil.copyfileobj(a, out, 8 << 20)
                    if size & 1:
                        out.write(b"\0")
                else:
                    out.write(c)
        if tmp.stat().st_size != riff_size + 8:
            return f"собранный файл {tmp.stat().st_size} Б ≠ {riff_size + 8} Б по заголовку"
        st = f.stat()
        tmp.rename(wav)
        os.utime(wav, (st.st_atime, st.st_mtime))
        f.unlink()
        return None
    finally:
        raw.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def restore_split(flac: str, f: Path, side: Path) -> str | None:
    """Сборка WAV, сжатого с отдельным файлом блоков (<имя>.ptchunks): начало файла
    из .ptchunks + звук из FLAC + конец файла из .ptchunks. FLAC и .ptchunks
    удаляются только если SHA-256 собранного файла совпал с исходным."""
    wav = f.with_suffix(".wav")
    raw = f.with_name(f.name + ".raw.part")
    tmp = wav.with_name(wav.name + ".part")
    try:
        with open(side, "rb") as fh:
            magic, off, n, size, digest = CHUNKS_HEAD.unpack(fh.read(CHUNKS_HEAD.size))
            if magic != CHUNKS_MAGIC:
                return f"{side.name}: неизвестный формат"
            head = fh.read(off)
            tail = fh.read()
        if len(head) != off or off + n + len(tail) != size:
            return f"{side.name}: повреждён (размеры не сходятся)"
        bps = flac_meta(f)["bps"]
        r = subprocess.run([flac, "-d", "-f", "-s", "--force-raw-format", "--endian=little",
                            "--sign=" + ("unsigned" if bps <= 8 else "signed"),
                            "-o", str(raw), str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            return r.stderr.strip()[-500:]
        if raw.stat().st_size != n:
            return f"размер звука {raw.stat().st_size:,} ≠ {n:,}"
        sha = hashlib.sha256(head)
        with open(tmp, "wb") as out, open(raw, "rb") as a:
            out.write(head)
            while chunk := a.read(8 << 20):
                sha.update(chunk)
                out.write(chunk)
            out.write(tail)
        sha.update(tail)
        if sha.digest() != digest:
            return "контрольная сумма собранного WAV не совпала с исходной"
        st = f.stat()
        tmp.rename(wav)
        os.utime(wav, (st.st_atime, st.st_mtime))
        f.unlink()
        side.unlink()
        return None
    except (OSError, ValueError, struct.error) as e:
        return str(e)
    finally:
        raw.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


# ------------------------------------------------- дубликаты по ссылкам .ptref --

def raw_audio(flac: str, target: Path, out: Path) -> str | None:
    """Звук (сырой PCM как в WAV) из FLAC или из блока data WAV -> out."""
    if target.suffix.lower() == ".flac":
        bps = flac_meta(target)["bps"]
        r = subprocess.run([flac, "-d", "-f", "-s", "--force-raw-format", "--endian=little",
                            "--sign=" + ("unsigned" if bps <= 8 else "signed"),
                            "-o", str(out), str(target)], capture_output=True, text=True)
        return None if r.returncode == 0 else r.stderr.strip()[-300:]
    info = wave_info(target)
    if not info["audio_len"]:
        return f"{target.name}: не найден звук"
    with open(target, "rb") as src, open(out, "wb") as dst:
        src.seek(info["audio_off"])
        left = info["audio_len"]
        while left:
            c = src.read(min(left, 8 << 20))
            if not c:
                return f"{target.name}: файл короче заголовка"
            dst.write(c)
            left -= len(c)
    return None


def restore_ref(flac: str, stub: Path, server_stub: Path | None) -> str | None:
    """Собирает файл по ссылке .ptref. Оригинал ищется рядом (в той же копии),
    а если его там нет — на сервере. Ссылка удаляется только при успехе."""
    h, head, tail = read_stub(stub)
    cands = [stub_target(stub, h)] + ([stub_target(server_stub, h)] if server_stub else [])
    target = next((c for c in cands if c.exists()), None)
    if target is None:  # оригинал в этой копии уже распакован в WAV
        target = next((c.with_suffix(".wav") for c in cands
                       if c.suffix.lower() == ".flac" and c.with_suffix(".wav").exists()), None)
    if target is None:
        return f"оригинал не найден: {h['target']}"
    st = stub.stat()
    if h["kind"] == "same":
        out = stub.with_name(h["name"])
        same_form = target.suffix.lower() == out.suffix.lower()
        if not same_form:  # оригинал уже распакован в WAV — это тот же исходный файл
            out = out.with_suffix(target.suffix)
        tmp = out.with_name(out.name + ".part")
        shutil.copyfile(target, tmp)
        if same_form and tmp.stat().st_size != h["size"]:
            tmp.unlink()
            return "размер копии не совпал"
    else:
        out = stub.with_name(h["out"])
        tmp = out.with_name(out.name + ".part")
        raw = stub.with_name(stub.name + ".raw.part")
        try:
            fail = raw_audio(flac, target, raw)
            if fail:
                return fail
            md5 = hashlib.md5()
            flip = bytes(x ^ 0x80 for x in range(256)) if h["bps"] <= 8 else None
            with open(tmp, "wb") as dst, open(raw, "rb") as a:
                dst.write(head)
                while c := a.read(8 << 20):
                    md5.update(c.translate(flip) if flip else c)
                    dst.write(c)
                dst.write(tail)
        finally:
            raw.unlink(missing_ok=True)
        if md5.hexdigest() != h["md5"] or tmp.stat().st_size != h["wav_size"]:
            tmp.unlink()
            return "звук оригинала не совпал со ссылкой"
    tmp.rename(out)
    os.utime(out, (st.st_atime, st.st_mtime))
    stub.unlink()
    return None


def local_original(stub: Path, work: Path) -> bool:
    """Оригинал ссылки лежит в восстанавливаемой папке (FLAC или уже WAV)."""
    try:
        h, _, _ = read_stub(stub)
    except (OSError, ValueError):
        return False
    t = stub_target(stub, h)
    if not t.is_relative_to(work):
        return False
    return t.exists() or (t.suffix.lower() == ".flac" and t.with_suffix(".wav").exists())


def step_refs(ui: UI, work: Path, server_src: Path | None, dry: bool,
              source_dups: bool = False, only_source: bool = False) -> int:
    """Собирает файлы по ссылкам .ptref. Ссылки вне сессий (Source и т.п.), чей
    оригинал есть в этой же папке (в сессии), без --source-dups остаются ссылками:
    второй экземпляр того же звука на диске не нужен; собрать их можно позже —
    pt_restore --source-dups на уже восстановленной папке."""
    stubs = sorted(p for p in work.rglob("*" + PTREF_EXT) if not p.name.startswith("._"))
    if only_source:  # сессии не трогаем: только ссылки вне них, и собираем все
        sessions = find_sessions([work])
        stubs = [p for p in stubs if not any(p.is_relative_to(s) for s in sessions)]
        source_dups = True
    if not source_dups and stubs:
        sessions = find_sessions([work])
        keep = [p for p in stubs
                if not any(p.is_relative_to(s) for s in sessions) and local_original(p, work)]
        if keep:
            ui.info(f"  Дубликаты в Source: оставлено ссылками {len(keep)} "
                    f"(оригиналы в сессиях; собрать WAV — --source-dups)")
            for p in keep:
                ui.debug(f"оставлена ссылка: {p.relative_to(work)}")
            stubs = [p for p in stubs if p not in keep]
    if not stubs:
        return 0
    if dry:
        ui.info(f"  Дубликаты: будет собрано по ссылкам {len(stubs)} файлов")
        return 0
    flac = shutil.which("flac") or "/opt/homebrew/bin/flac"
    bar = ui.bar("  Дубликаты", len(stubs))
    errors = 0
    for stub in stubs:
        rel = stub.relative_to(work)
        try:
            fail = restore_ref(flac, stub, server_src / rel if server_src else None)
        except (OSError, ValueError, KeyError) as e:
            fail = str(e)
        if fail:
            errors += 1
            ui.error(f"{rel}: {fail}")
        else:
            ui.debug(f"собран по ссылке: {rel.with_suffix('')}")
        bar.advance()
    bar.close("готово" if not errors else f"ошибок: {errors}")
    return errors


# ------------------------------------------- рабочие копии видео (.ptwork) --

def step_workcopy(ui: UI, work: Path, server_src: Path | None, dry: bool, enabled: bool) -> int:
    """Пересобирает рабочие копии *_Workcopy_*.mov через mix_prep по Workcopies.ptwork.
    Собранные записи убираются из файла, файл удаляется, когда записей не осталось.
    Без --workcopy описания остаются на месте (можно собрать позже)."""
    files = sorted(p for p in work.rglob(PTWORK_FILE))
    todo = []
    for pw in files:
        try:
            entries = json.loads(pw.read_text(encoding="utf-8")).get("workcopies", {})
        except (OSError, ValueError) as e:
            ui.error(f"{pw}: {e}")
            continue
        todo += [(pw, key, e) for key, e in entries.items() if not (pw.parent / key).exists()]
    if not todo:
        for pw in files:  # все копии уже на месте — описание больше не нужно
            if not dry:
                pw.unlink()
        return 0
    if not enabled:
        ui.info(f"  Видео: рабочих копий не пересобрано {len(todo)} (собрать — --workcopy)")
        return 0
    if dry:
        ui.info(f"  Видео: будет пересобрано рабочих копий {len(todo)}")
        return 0

    def where(pw: Path, rel: str) -> Path | None:
        """Файл рядом с Workcopies.ptwork: в восстановленной папке или на сервере."""
        cands = [Path(os.path.normpath(pw.parent / rel))]
        if server_src:
            cands.append(Path(os.path.normpath((server_src / pw.relative_to(work)).parent / rel)))
        return next((c for c in cands if c.exists()), None)

    errors = 0
    done: dict[Path, list[str]] = {}
    for k, (pw, key, e) in enumerate(todo, 1):
        out = pw.parent / key
        source = where(pw, e["source"])
        if source is None:
            ui.error(f"{out.name}: исходное видео не найдено ({e['source']})")
            errors += 1
            continue
        ui.info(f"  Видео {k}/{len(todo)}: {out.name} ← {source.name}")
        if not e.get("params_known", True):
            ui.warn(f"{out.name}: сделана старым mix_prep — таймкод и субтитры неизвестны, "
                    "собирается без них")
        cmd = [sys.executable, MIX_PREP, str(source), "--output", str(out), "--no-surround"]
        if e.get("audio_index") is not None:
            cmd += ["--audio-index", str(e["audio_index"])]
        if e.get("audio_24bit"):
            cmd.append("--24")
        if e.get("timecode"):
            cmd.append("--timecode")
        if e.get("subtitles"):
            subs = where(pw, e["subtitles"])
            if subs is None:
                ui.error(f"{out.name}: файл субтитров не найден ({e['subtitles']})")
                errors += 1
                continue
            cmd += ["--subtitles", str(subs)]
        out.parent.mkdir(parents=True, exist_ok=True)
        ui.log(" ".join(cmd))
        with ui.lock:  # прогресс рисует сам mix_prep
            rc = subprocess.run(cmd, cwd=out.parent).returncode
        info = video_probe(out) if rc == 0 and out.exists() else None
        if not info or info["frames"] != e["frames"]:
            ui.error(f"{out.name}: mix_prep завершился с ошибкой или число кадров не совпало "
                     f"({info['frames'] if info else '—'} ≠ {e['frames']}); запись оставлена")
            errors += 1
            continue
        done.setdefault(pw, []).append(key)
        ui.debug(f"пересобрано: {out}")
    for pw, keys in done.items():  # убираем собранные записи
        entries = json.loads(pw.read_text(encoding="utf-8")).get("workcopies", {})
        for key in keys:
            entries.pop(key, None)
        if entries:
            pw.write_text(json.dumps({"v": 1, "workcopies": entries}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        else:
            pw.unlink()
    return errors


# ------------------------------- видео сессий, не попавшее в распаковку --

VIDEO_FILES = "Video Files"  # папка видео внутри сессии, как у Pro Tools: там он ищет пропавший файл


def server_ancestors(src: Path, v: Path):
    """Предки src до корня шары и путь v (из .ptx) от них, если в v есть папка с тем же именем:
    /…/MyFilm/Ep02/Audio Files/x.wav для предка «MyFilm» на сервере — MyFilm/Ep02/Audio Files/x.wav."""
    for a in [src, *src.parents]:
        if len(a.parts) <= 3:  # /Volumes/<шара> — выше не ищем
            break
        mapped = None
        if a.name in v.parts[:-1]:
            i = len(v.parts) - 1 - v.parts[::-1].index(a.name)
            mapped = a.joinpath(*v.parts[i + 1:])
        yield a, mapped


def project_of(src: Path) -> Path:
    return src.parent if is_session(src) else src


def server_video(src: Path, v: Path) -> tuple[Path, Path | None, dict | None] | None:
    """Где на сервере видео v (путь из .ptx), если распакована только часть проекта.
    Поднимается от src к корню шары: та же папка по имени предка, Video (вне проекта)/,
    либо рабочая копия, от которой на сервере осталась запись Workcopies.ptwork.
    Возвращает (файл, исходник рабочей копии или None, запись .ptwork или None)."""
    ext = v.parts[1:]
    ext = ext[1:] if ext[:1] == ("Volumes",) else ext
    for a, mapped in server_ancestors(src, v):
        cands = ([mapped] if mapped else []) + ([a / EXTERNAL_VIDEO_DIR / Path(*ext)] if ext else [])
        for c in cands:
            if c.is_file():
                return c, None, None
            w = ptwork_entry_for(c)
            if w and (w[0].parent / w[1]["source"]).is_file():
                return c, w[0].parent / w[1]["source"], w[1]
    # проект переименован — по имени в папке проекта, где сессии, и в её подпапках (Video и т.п.)
    proj = project_of(src)
    if len(proj.parts) > 3:
        for c in [proj / v.name, *(d / v.name for d in sorted(proj.iterdir()) if d.is_dir())]:
            if c.is_file():
                return c, None, None
    return None


def copy_files(ui: UI, what: str, files: list[Path], out: Path) -> list[Path]:
    """Файлы с сервера в папку out одним rsync, с общей полосой прогресса (полоса на каждый
    файл мерцает, когда их сотни). Возвращает файлы, которые не скопировались."""
    if not files:
        return []
    rsync, progress2 = find_rsync()
    flags = ["-a", f"--partial-dir={PARTIAL_DIR}"] + (["--info=progress2"] if progress2 else [])
    out.mkdir(parents=True, exist_ok=True)
    total = sum(x.stat().st_size for x in files if x.exists())
    bar = ui.bar(f"  {what} · {len(files)} файлов" if len(files) > 1 else f"  {what} · {files[0].name}",
                 total, "bytes" if progress2 else "pct")
    rc, err, _ = run_rsync(ui, bar, [rsync, *flags, *map(str, files), str(out) + "/"])
    failed = [x for x in files
              if not (out / x.name).exists() or (out / x.name).stat().st_size != x.stat().st_size]
    if rc != 0 or failed:
        bar.close(f"ошибка rsync ({rc}), не скопировано: {len(failed)}")
        ui.error(f"{out}: {err.strip()[-300:]}")
    else:
        bar.close(f"готово → {out.parent.name}/{out.name}/")
    return failed


def audio_root(src: Path, v: Path) -> Path:
    """Корень проекта на сервере для файла v: самый верхний предок src, чьё имя есть в пути из .ptx
    (Bleach/sessions/… и Bleach/work/… — один проект «Bleach»); иначе папка, где лежат сессии."""
    hits = [a for a, mapped in server_ancestors(src, v) if mapped]
    return hits[-1] if hits else project_of(src)


def name_index(root: Path) -> dict[str, list[Path]]:
    """Аудио на сервере в том виде, как его сохранил бэкап, по имени исходного файла (без регистра)."""
    idx: dict[str, list[Path]] = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != BACKUPS_DIR)
        for f in files:
            p = Path(d) / f
            if f.startswith("._") or not (is_audio(p) or p.suffix == PTREF_EXT):
                continue
            for ext in ((".wav", ".WAV") if p.suffix.lower() in (".flac", PTREF_EXT) else (p.suffix,)):
                idx.setdefault(p.with_suffix(ext).name.lower(), []).append(p)
    return idx


def chunk_walk(buf: bytes, pos: int = 0):
    """Чанки RIFF подряд в buf: (id, содержимое)."""
    while pos + 8 <= len(buf):
        cid, n = buf[pos:pos + 4], struct.unpack_from("<I", buf, pos + 4)[0]
        yield cid, buf[pos + 8:pos + 8 + n]
        pos += 8 + n + (n & 1)


def pt_ids(p: Path) -> set[bytes]:
    """ID файла, по которым Pro Tools узнаёт его в сессии: 12 байт из чанка umid (пишет сам Pro Tools)
    и 8 байт материала UMID из bext. Читаются только заголовки: FLAC — сохранённые чанки WAV,
    WAV — чанки с переходом через data, .ptref — заголовок из ссылки или её оригинал."""
    chunks: list[tuple[bytes, bytes]] = []
    try:
        if p.suffix.lower() == ".flac":
            for blk in flac_meta(p)["riff"]:
                if blk[:4] in (b"umid", b"bext"):
                    chunks += chunk_walk(bytes(blk))
        elif p.suffix == PTREF_EXT:
            h, head, tail = read_stub(p)
            if h["kind"] == "same":
                return pt_ids(stub_target(p, h))
            chunks += chunk_walk(head, 12)
            chunks += chunk_walk(tail)
        else:
            with _quiet_close(p) as f:
                if f.read(12)[:4] != b"RIFF":
                    return set()
                while len(hdr := f.read(8)) == 8:
                    cid, n = hdr[:4], struct.unpack("<I", hdr[4:])[0]
                    if cid in (b"umid", b"bext"):
                        chunks.append((cid, f.read(n)))
                        f.seek(n & 1, 1)
                    else:
                        f.seek(n + (n & 1), 1)
    except (OSError, ValueError, KeyError, IndexError):
        return set()
    out = set()
    for cid, d in chunks:
        if cid == b"umid" and len(d) >= 16:
            out.add(d[4:16])
        elif cid == b"bext" and len(d) >= 412 and any(d[348:412]):
            out.add(d[364:372])
    return out


def server_audio(src: Path, v: Path, indexes: dict[Path, dict], ptx_data: bytes) -> tuple[Path, bool] | None:
    """Где на сервере аудио v (путь из .ptx) в том виде, как его сохранил бэкап (WAV, FLAC или .ptref):
    по тому же пути от предка src, иначе по имени где угодно в проекте (индекс строится один раз).
    Подходит файл, чей ID Pro Tools (pt_ids) есть в расшифрованном .ptx; файл с другим ID — другой,
    пусть и с тем же именем. Файл без ID (сделан не в Pro Tools) — только по имени, если с ID ничего нет.
    Возвращает (файл, сверен ли по ID)."""
    cands = []
    for _, mapped in server_ancestors(src, v):
        if mapped and (f := next((c for c in (mapped, *audio_twin(mapped)) if c.is_file()), None)):
            cands.append(f)
    root = audio_root(src, v)
    if len(root.parts) > 3:
        if root not in indexes:
            indexes[root] = name_index(root)
        cands += sorted((h for h in indexes[root].get(v.name.lower(), []) if not h.is_relative_to(src)),
                        key=lambda h: (h.parent.name != v.parent.name, str(h)))
    by_name = None
    for c in dict.fromkeys(cands):
        ids = pt_ids(c)
        if not ids:
            by_name = by_name or c
        elif any(i in ptx_data for i in ids):
            return c, True
    return (by_name, False) if by_name else None


def step_audio(ui: UI, work: Path, src: Path, jobs: int, dry: bool) -> tuple[int, list[Path]]:
    """Аудио, на которое ссылаются распакованные сессии, но которого нет ни по пути из .ptx, ни в
    распакованной папке (лежало в другой сессии проекта и т.п.), — с сервера в <сессия>/Audio Files/;
    FLAC и .ptref тут же распаковываются. Запускается после распаковки сессий: распакованные WAV
    уже на месте и находятся. Поиск по имени — по всему проекту на сервере (сессии, work/, old_mix/ …).
    Возвращает (ошибок, полученные WAV — для .pt_restored.json)."""
    todo: dict[str, tuple[Path, Path, bool]] = {}  # имя файла → (сессия, файл на сервере, сверен по ID)
    missing: set[Path] = set()
    indexes: dict[Path, dict] = {}  # обход проекта на сервере — только если что-то пропало
    for sess in find_sessions([work]):
        for ptx in sorted(sess.glob("*.ptx")):
            if ptx.name.startswith("._"):
                continue
            files = session_media(ptx, 0x1004)
            raw = ptx.read_bytes()
            ptx_data = ptx_crypt(raw) or raw
            if files is None:
                ui.warn("конвертер Reaper ↔ Pro Tools не найден — аудио вне сессий не ищется")
                return 0, []
            for v, ok in files:
                if ok or v.name in todo or v in missing:
                    continue
                found = server_audio(src, v, indexes, ptx_data)
                if found is None:
                    missing.add(v)
                elif not found[0].is_relative_to(src):  # внутри src — распаковывается вместе с ним
                    todo[v.name] = (sess, *found)
    if missing:
        ui.warn(f"{work.name}: аудио сессии не найдено ни на месте, ни на сервере: {len(missing)}"
                + (" (список — в подробном логе)" if len(missing) > 3 else
                   " — " + ", ".join(sorted(v.name for v in missing))))
        for v in sorted(missing):
            ui.debug(f"не найдено аудио: {v}")
    if not todo:
        return 0, []
    unverified = sorted(n for n, (_, _, by_id) in todo.items() if not by_id)
    name_only = f" (без ID Pro Tools, только по имени: {len(unverified)})" if unverified else ""
    for n in unverified:
        ui.debug(f"по имени, без ID: {n} ← {todo[n][1]}")
    if dry:
        ui.info(f"  Аудио из других папок проекта: будет скопировано {len(todo)}{name_only}")
        return 0, []
    ui.header("Аудио из других папок проекта:", f"{len(todo)} файлов{name_only}")
    errors = 0
    flacs, stubs, made = [], [], []
    plan: dict[Path, list[Path]] = {}  # папка Audio Files сессии → файлы с сервера (с .ptchunks)
    for name, (sess, f, _) in sorted(todo.items()):
        out = sess / AUDIO_DIR
        if (out / f.name).exists():
            ui.warn(f"{sess.name}: {AUDIO_DIR}/{f.name} уже есть, другой файл с тем же именем — не копирую")
            continue
        side = f.with_suffix(CHUNKS_EXT)
        plan.setdefault(out, []).extend([f] + ([side] if f.suffix.lower() == ".flac" and side.is_file() else []))
    for out, files in plan.items():
        failed = set(copy_files(ui, "Аудио", files, out))
        errors += len(failed)
        for f in files:
            local = out / f.name
            if f in failed or f.suffix == CHUNKS_EXT or (f.suffix.lower() == ".flac" and f.with_suffix(CHUNKS_EXT) in failed):
                continue
            if local.suffix.lower() == ".flac":
                flacs.append(local)
            elif local.suffix == PTREF_EXT:
                stubs.append((local, f))
            made.append(local.with_suffix(".wav") if local.suffix.lower() in (".flac", PTREF_EXT) else local)
    flac = shutil.which("flac") or "/opt/homebrew/bin/flac"
    for stub, server_stub in stubs:  # оригинал — на сервере, рядом с серверной ссылкой
        try:
            h, _, _ = read_stub(stub)
            fail = restore_ref(flac, stub, server_stub)
        except (OSError, ValueError, KeyError) as e:
            fail = str(e)
        if fail:
            ui.error(f"{stub.name}: {fail}")
            errors += 1
            continue
        got = stub.with_name(h.get("out") or h["name"])
        if got.suffix.lower() == ".flac" or (not got.exists() and got.with_suffix(".flac").exists()):
            flacs.append(got.with_suffix(".flac"))
    if flacs:
        errors += step_decode(ui, work, jobs, False, "  FLAC → WAV", files=flacs)
    return errors, made


def step_video(ui: UI, work: Path, src: Path, dry: bool, enabled: bool) -> int:
    """Видео, на которое ссылаются распакованные сессии, но которого нет ни по пути из .ptx,
    ни в распакованной папке (распакованы отдельные сессии, а видео лежит в проекте рядом
    или вне проекта), — копируется с сервера в <сессия>/Video Files/. Рабочая копия mov,
    от которой на сервере только описание, — её исходник + Workcopies.ptwork (собрать — --workcopy)."""
    todo: dict[Path, tuple[Path, Path, Path | None, dict | None]] = {}
    missing = []
    linked = 0
    for sess in find_sessions([work]):
        for ptx in sorted(sess.glob("*.ptx")):
            if ptx.name.startswith("._"):
                continue
            vids = session_media(ptx)
            if vids is None:
                (ui.warn if enabled else ui.debug)("конвертер Reaper ↔ Pro Tools не найден — видео сессий не ищется")
                return 0
            linked += len(vids)
            for v, ok in vids:
                if ok or v in todo:
                    continue
                found = server_video(src, v)
                if found is None:
                    missing.append(v)
                elif not found[0].is_relative_to(src):  # внутри src — уже скопировано
                    todo[v] = (sess, *found)
    for v in sorted(set(missing)):
        ui.warn(f"{work.name}: видео сессии не найдено ни на месте, ни на сервере — {v}")
    if not todo:
        if enabled:
            ui.info("  Видео: в сессиях не прилинковано" if not linked else
                    "  Видео: всё на месте, копировать нечего" if not missing else
                    "  Видео: на сервере не найдено")
        return 0
    if not enabled:
        ui.info(f"  Видео сессий вне распакованной папки: {len(todo)} (скопировать — --video)")
        return 0
    if dry:
        for v, (sess, f, source, _) in todo.items():
            ui.info(f"  Видео: будет скопировано {(source or f).name} → {sess.name}/{VIDEO_FILES}/")
        return 0
    errors = 0
    for v, (sess, f, source, entry) in todo.items():
        out = sess / VIDEO_FILES
        files = [source or f]
        if entry and entry.get("subtitles"):
            files.append(Path(os.path.normpath(source.parent / entry["subtitles"])))
        bad = len(copy_files(ui, "Видео", files, out))
        errors += bad
        if entry and not bad:  # рабочую копию соберёт step_workcopy рядом с исходником
            pw = out / PTWORK_FILE
            try:
                entries = json.loads(pw.read_text(encoding="utf-8")).get("workcopies", {})
            except (OSError, ValueError):
                entries = {}
            entries[f.name] = {**entry, "source": source.name,
                               "subtitles": Path(entry["subtitles"]).name if entry.get("subtitles") else None}
            pw.write_text(json.dumps({"v": 1, "workcopies": entries}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return errors


# ------------------------------------------------------------------- шаги --

def made_by_backup(f: Path) -> bool:
    """FLAC сделан бэкапом из WAV: внутри сохранён заголовок WAV или рядом .ptchunks.
    FLAC, пришедшие как FLAC (например, от клиента), такого заголовка не имеют."""
    if f.with_suffix(CHUNKS_EXT).exists():
        return True
    try:
        riff = flac_meta(f)["riff"]
    except (OSError, ValueError, IndexError):
        return False
    return bool(riff)


def loose_flacs(work: Path, sessions: list[Path]) -> list[Path]:
    """FLAC от бэкапа вне сессий (Source и т.п.) — где бы ни лежали."""
    out = []
    for d, _, files in os.walk(work):
        dp = Path(d)
        if any(dp == s or dp.is_relative_to(s) for s in sessions):
            continue
        out += [dp / f for f in files
                if f.lower().endswith(".flac") and not f.startswith("._") and made_by_backup(dp / f)]
    return sorted(out)


def step_decode(ui: UI, session: Path, jobs: int, dry: bool, title: str,
                dirs: list[Path] | None = None, files: list[Path] | None = None) -> int:
    """FLAC -> WAV в FLAC_DIRS сессии, в папках dirs или в списке files."""
    if dirs is None:
        dirs = [session / sub for sub in FLAC_DIRS]
    flacs = files if files is not None else sorted(
        p for d in dirs if d.is_dir() for p in d.rglob("*")
        if p.is_file() and p.suffix.lower() == ".flac" and not p.name.startswith("._"))
    if files is None:
        # FLAC, которые сделал не бэкап (пришли как FLAC), не трогаем: распаковка
        # удалила бы их, а WAV из них — не исходный файл сессии
        theirs = [f for f in flacs if not made_by_backup(f)]
        if theirs:
            flacs = [f for f in flacs if f not in set(theirs)]
            ui.info(f"{title}: оставлены как есть {len(theirs)} FLAC — сделаны не бэкапом")
            for f in theirs:
                ui.debug(f"не от бэкапа: {f}")
    if not flacs:
        ui.info(f"{title}: нет FLAC-файлов")
        return 0
    if dry:
        for f in flacs:
            ui.debug(f"flac -d {f}")
        ui.info(f"{title}: будет распаковано {len(flacs)} FLAC "
                f"({fmt_bytes(sum(f.stat().st_size for f in flacs))})")
        return 0

    flac = shutil.which("flac") or "/opt/homebrew/bin/flac"
    sizes = {f: f.stat().st_size for f in flacs}
    bar = ui.bar(title, sum(sizes.values()), "bytes")
    errors = 0

    def decode(f: Path):
        side = f.with_suffix(CHUNKS_EXT)
        if side.exists():  # сжат с отдельным файлом блоков
            fail = restore_split(flac, f, side)
            return f, (0 if fail is None else 1), (fail or "собран из FLAC + " + side.name)
        # имя задаём явно: для RF64 flac сам назвал бы результат .rf64,
        # а сессия Pro Tools ссылается на исходное имя .wav
        p = popen([flac, *FLAC_FLAGS, "-s", "-o", str(f.with_suffix(".wav")), str(f)],
                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            _, err = p.communicate()
        finally:
            forget(p)
        if p.returncode != 0 and "foreign metadata" in err:
            # flac не смог повторить исходный заголовок — собираем WAV сами
            fail = rebuild_wav(flac, f)
            if fail is None:
                return f, 0, "заголовок WAV восстановлен из сохранённых блоков"
            err += f"\n    ручная сборка: {fail}"
        return f, p.returncode, err

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for fut in as_completed([ex.submit(decode, f) for f in flacs]):
            f, rc, err = fut.result()
            if rc == 0:
                ui.debug(f"WAV ✓ {f.with_suffix('.wav')}" + (f" ({err})" if err else ""))
            else:
                errors += 1
                ui.error(f"flac -d ({rc}): {f}\n    {err.strip()[-500:]}")
            bar.advance(sizes[f])
    bar.close("готово" if not errors else f"ошибок: {errors}")
    return errors


def backup_zips(session: Path) -> list[Path]:
    """Архивы pt_backup: "Session File Backups.zip" и "... ГГГГММДД-ЧЧММСС.zip",
    от старых к новым — более поздние бэкапы распаковываются последними."""
    zips = [p for p in session.glob(f"{BACKUPS_DIR}*") if p.is_file()
            and p.suffix.lower() in (".zip", PTBAK_EXT)]
    return sorted(zips, key=lambda p: p.stat().st_mtime)


def entry_target(out_dir: Path, info: zipfile.ZipInfo) -> Path | None:
    """Путь для файла из архива внутри out_dir; None — если путь выходит наружу."""
    parts = Path(info.filename).parts
    if parts and parts[0] == BACKUPS_DIR:
        parts = parts[1:]
    target = (out_dir.joinpath(*parts)).resolve()
    return target if target.is_relative_to(out_dir.resolve()) else None


def dated_name(p: Path, info: zipfile.ZipInfo) -> Path:
    """S1.bak.001.ptx -> S1.bak.001_2026-09-29_14-30-05.ptx (дата из архива).

    Pro Tools после удаления папки бэкапов начинает нумерацию заново, поэтому
    в разных архивах могут быть файлы с одинаковыми именами."""
    ts = "{:04d}-{:02d}-{:02d}_{:02d}-{:02d}-{:02d}".format(*info.date_time)
    cand = p.with_name(f"{p.stem}_{ts}{p.suffix}")
    i = 2
    while cand.exists():
        cand = p.with_name(f"{p.stem}_{ts}_{i}{p.suffix}")
        i += 1
    return cand


def same_as_entry(p: Path, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    if p.stat().st_size != info.file_size:
        return False
    with open(p, "rb") as a, zf.open(info) as b:
        while True:
            da, db = a.read(1 << 20), b.read(1 << 20)
            if da != db:
                return False
            if not da:
                return True


def extract_zip(ui: UI, zpath: Path, out_dir: Path, bar) -> int:
    """Распаковка с проверкой CRC. Совпадающие файлы пропускаются, одноимённые
    с другим содержимым сохраняются с датой в имени. Возвращает число ошибок."""
    errors = 0
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            target = entry_target(out_dir, info)
            if target is None:
                errors += 1
                ui.error(f"{zpath.name}: небезопасный путь в архиве: {info.filename}")
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if same_as_entry(target, zf, info):
                    ui.debug(f"уже есть: {target.name}")
                    bar.advance(info.file_size)
                    continue
                target = dated_name(target, info)
                ui.debug(f"имя занято, сохраняю как {target.name}")
            tmp = target.with_name(target.name + ".part")
            try:
                with zf.open(info) as src, open(tmp, "wb") as dst:  # CRC проверяется при чтении
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                        bar.advance(len(chunk))
                tmp.rename(target)
                mtime = time.mktime(info.date_time + (0, 0, -1))
                os.utime(target, (mtime, mtime))
            except (OSError, zipfile.BadZipFile) as e:
                errors += 1
                tmp.unlink(missing_ok=True)
                ui.error(f"{zpath.name}: {info.filename}: {e}")
    return errors


def ptbak_manifest(path: Path) -> dict:
    """Первый элемент .ptbak — описание (число файлов и объём): читаем только его."""
    p = subprocess.Popen([ZSTD, "-d", "-q", "--long=31", "-c", str(path)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            ti = tf.next()
            if ti is not None and ti.name == PTBAK_MANIFEST:
                return json.loads(tf.extractfile(ti).read())
    except (tarfile.TarError, ValueError):
        pass
    finally:
        p.kill()
        p.wait()
    return {}


def extract_ptbak(ui: UI, path: Path, out_dir: Path, bar) -> int:
    """Распаковка .ptbak: zstd -> tar; с .ptx возвращается XOR-шифр Pro Tools.
    Совпадающие файлы пропускаются, одноимённые с другим содержимым — с датой в имени."""
    errors = 0
    p = popen([ZSTD, "-d", "-q", "--long=31", "-c", str(path)],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            for ti in tf:
                if not ti.isfile() or ti.name == PTBAK_MANIFEST:
                    continue
                parts = Path(ti.name).parts
                if parts and parts[0] == BACKUPS_DIR:
                    parts = parts[1:]
                target = out_dir.joinpath(*parts).resolve()
                if not parts or not target.is_relative_to(out_dir.resolve()):
                    errors += 1
                    ui.error(f"{path.name}: небезопасный путь в архиве: {ti.name}")
                    continue
                data = tf.extractfile(ti).read()
                if ti.pax_headers.get("PTBAK.xor") == "1":
                    data = ptx_crypt(data)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.read_bytes() == data:
                        bar.advance(len(data))
                        continue
                    ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(ti.mtime))
                    cand, i = target.with_name(f"{target.stem}_{ts}{target.suffix}"), 2
                    while cand.exists():
                        cand = target.with_name(f"{target.stem}_{ts}_{i}{target.suffix}")
                        i += 1
                    target = cand
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(data)
                tmp.rename(target)
                os.utime(target, (ti.mtime, ti.mtime))
                bar.advance(len(data))
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            errors += 1
            ui.error(f"{path.name}: zstd: {err.strip()[-300:]}")
    except (tarfile.TarError, OSError) as e:
        errors += 1
        ui.error(f"{path.name}: {e}")
    finally:
        forget(p)
    return errors


def step_backups(ui: UI, session: Path, dry: bool, title: str) -> int:
    zips = backup_zips(session)
    if not zips:
        ui.info(f"{title}: нет архивов")
        return 0
    out_dir = session / BACKUPS_DIR
    total = 0
    for z in zips:
        if z.suffix.lower() == PTBAK_EXT:
            total += ptbak_manifest(z).get("bytes", 0)
        else:
            try:
                total += sum(i.file_size for i in zipfile.ZipFile(z).infolist())
            except zipfile.BadZipFile as e:
                ui.error(f"{title}: повреждённый архив {z.name}: {e}")
                return 1
    if dry:
        for z in zips:
            ui.debug(f"распаковка {z.name} -> {BACKUPS_DIR}/, затем rm")
        ui.info(f"{title}: будет распаковано архивов: {len(zips)} ({fmt_bytes(total)})")
        return 0

    bar = ui.bar(title, total, "bytes")
    errors = 0
    for z in zips:
        e = (extract_ptbak(ui, z, out_dir, bar) if z.suffix.lower() == PTBAK_EXT
             else extract_zip(ui, z, out_dir, bar))
        if e:
            errors += e
            ui.error(f"{z.name}: распакован с ошибками, архив не удалён")
        else:
            z.unlink()
            ui.debug(f"{z.name} -> {BACKUPS_DIR}/")
    bar.close("готово" if not errors else f"ошибок: {errors}")
    return errors


# ------------------------------------------------ копирование на локальный диск --


def tree_size(root: Path) -> int:
    return sum((Path(d) / f).lstat().st_size for d, _, fs in os.walk(root) for f in fs)


def free_space(path: Path) -> int:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def share_alive(ui: UI, src: Path) -> bool:
    """Сетевая папка отвечает (ls с таймаутом); если нет — шара отключается и
    подключается заново, как в pt_backup. Без этого обход зависшей шары висит вечно."""
    if not is_network(src) or run_quiet(["/bin/ls", str(src)], 20) is not None:
        return True
    ui.warn(f"шара не отвечает — переподключение: {src}")
    smb = make_smb(ui, src, SMB_HOST, SMB_USER, SMB_PASS, None, 1, 5)
    if smb:
        mp = smb.mount_point()
        if mp:
            smb._unmount(mp)
        smb._mount()
    ok = run_quiet(["/bin/ls", str(src)], 20) is not None
    if not ok:
        ui.error(f"шара не отвечает: {src}")
    return ok


def step_copy(ui: UI, src: Path, to_dir: Path, dry: bool, title: str,
              skip_dirs: list[Path] = ()) -> Path | None:
    """Копирует src в to_dir/<имя> (кроме skip_dirs) и возвращает путь копии."""
    dst = to_dir / src.name
    if dst.resolve() == src:
        ui.info(f"{title}: папка уже на локальном диске, работаю с ней")
        return src
    if to_dir.resolve().is_relative_to(src):
        ui.error(f"{title}: нельзя копировать папку внутрь самой себя ({to_dir})")
        return None

    # в уже распакованной копии WAV есть — соответствующие FLAC не нужны
    excludes = [f"/{src.name}/{f.relative_to(src).with_suffix(ext)}"
                for pat in ("*.flac", "*" + PTREF_EXT) for f in src.rglob(pat)
                for ext in (".flac", CHUNKS_EXT, PTREF_EXT)
                if (dst / f.relative_to(src)).with_suffix(".wav").exists()
                and (src / f.relative_to(src).with_suffix(ext)).exists()] \
        if dst.is_dir() else []
    size = tree_size(src)
    # объём, который реально уйдёт: без исключённых FLAC и без уже скопированного
    # (то же правило, что у rsync: размер + дата)
    skip = {e.split("/", 2)[2] for e in excludes}
    # папки, которые не копируем (сессии при --only-source)
    excludes += [f"/{src.name}/{d.relative_to(src)}/" for d in skip_dirs]
    todo = 0
    for d, dirs, fs in os.walk(src):
        dirs[:] = [x for x in dirs if Path(d) / x not in skip_dirs]
        for name in fs:
            p = Path(d) / name
            rel = str(p.relative_to(src))
            if rel in skip or PARTIAL_DIR in p.parts:
                continue
            s, q = p.lstat(), dst / rel
            try:
                t = q.lstat()
                if t.st_size == s.st_size and abs(t.st_mtime - s.st_mtime) <= 2:
                    continue
            except OSError:
                pass
            todo += s.st_size
    free = free_space(to_dir)
    if free < todo:
        ui.error(f"{title}: мало места в {to_dir}: нужно {fmt_bytes(todo)}, "
                 f"свободно {fmt_bytes(free)}")
        return None
    ui.debug(f"{src} -> {dst} ({fmt_bytes(size)}, свободно {fmt_bytes(free)})")
    if excludes:
        ui.debug(f"уже распаковано в копии, не копирую: {len(excludes)} FLAC")

    rsync, progress2 = find_rsync()
    flags = ["-av", f"--partial-dir={PARTIAL_DIR}", f"--exclude={PARTIAL_DIR}",
             f"--exclude={DEDUP_INDEX}"]  # справочник дубликатов нужен только на сервере
    if progress2:
        flags += ["--info=progress2", "--no-inc-recursive"]
    if dry:
        flags.append("-n")
    else:
        to_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".excl", delete=False) as xf:
        xf.write("\n".join(excludes) + "\n")
    state: dict = {}

    def attempt(n: int, retry: str):
        if n > 1 and not share_alive(ui, src):
            return None
        bar = (ui.bar(f"{title}{retry}", todo, "bytes") if progress2
               else ui.bar(f"{title}{retry}", 100, "pct"))
        rc, err, copied = run_rsync(ui, bar, [rsync, *flags, f"--exclude-from={xf.name}",
                                              str(src), str(to_dir) + "/"])
        state.update(bar=bar, copied=copied)
        return rc, err, bar

    try:
        ok = rsync_retry(ui, COPY_RETRIES, 5, attempt, lambda: share_alive(ui, src))
    finally:
        os.unlink(xf.name)
    if not ok:
        return None
    state["bar"].set(state["bar"].total)
    state["bar"].close(f"готово, скопировано файлов: {state['copied']}" if progress2 else "готово")
    return dst


# -------------------------------------------------------------------- main --

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Распаковка сессий Pro Tools после pt_backup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Примеры:", 1)[1])
    ap.add_argument("paths", nargs="+", type=Path,
                    help="папка сессии или проекта с несколькими сессиями (не меняется)")
    ap.add_argument("--source-dups", action="store_true",
                    help="собрать и дубликаты в Source (по умолчанию, если оригинал есть "
                         "в сессии, в Source остаётся ссылка .ptref)")
    ap.add_argument("--workcopy", action="store_true",
                    help="пересобрать рабочие копии видео *_Workcopy_*.mov через mix_prep "
                         "(по описаниям .ptwork; занимает время)")
    ap.add_argument("--video", action="store_true",
                    help="скопировать с сервера видео распакованных сессий, лежащее вне "
                         "распакованной папки (распакованы отдельные сессии), в <сессия>/Video Files/")
    ap.add_argument("--only-source", action="store_true",
                    help="восстановить из проекта только то, что вне сессий (Source и т.п.); "
                         "звук для дубликатов берётся из сессий на сервере")
    ap.add_argument("--copy", action="store_true",
                    help="копировать в --to и папки с локального диска (иначе — на месте)")
    ap.add_argument("--to", type=Path, default=Path(RESTORE_TO),
                    help=f"куда копировать перед распаковкой (по умолчанию {RESTORE_TO})")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 4),
                    help="параллельных процессов flac (по умолчанию 8: остальные ядра — Pro Tools и rsync)")
    ap.add_argument("--skip", action="append", choices=STEPS, default=[],
                    help="пропустить шаг (можно несколько раз)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="показать, что будет сделано, ничего не меняя")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    ap.add_argument("--plain", action="store_true",
                    help="построчный прогресс (автоматически, если вывод не в терминал)")
    ap.add_argument("--log", type=Path, default=default_log("pt_restore"),
                    help="лог-файл (по умолчанию ~/Library/Logs/pt_restore.log)")
    ap.add_argument("--no-log", action="store_true", help="не писать лог-файл")
    ap.add_argument("--gui", action="store_true",
                    help="события строками JSON для окна PT Backup (вкладка «Разархивация»)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except AttributeError:
        pass
    ui = UI(args.verbose, args.plain, None if args.no_log else args.log.expanduser(), gui=args.gui)
    started, rc = time.time(), 1
    try:
        rc = restore(args, ui)
        return rc
    except KeyboardInterrupt:
        rc = 130
        raise
    finally:  # общая история запусков с pt_backup (вкладка «История» окна PT Backup)
        record_run(args, ui, started, rc, mode="restore", dest=str(args.to.expanduser()))


def write_restored(work: Path, origin: Path | None, made: list[Path]):
    """Запись для pt_backup: откуда распакован проект и какие WAV получились распаковкой
    (размер и дата). Дописывается к прежней — например, после --only-source."""
    info = read_restored(work)
    files = info.get("files", {})
    for w in made:
        try:
            st = w.stat()
        except OSError:  # не распаковался — пусть бэкап сожмёт его как обычно
            continue
        files[name_key(w.relative_to(work))] = [st.st_size, int(st.st_mtime)]
    info.update(v=1, files=files, restored=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if origin:
        info["origin"] = str(origin)
    try:
        (work / RESTORED_FILE).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def restore(args, ui: UI) -> int:

    def on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    roots = [p.expanduser().resolve() for p in args.paths]
    for r in roots:
        if not r.is_dir():
            ui.error(f"папка не найдена: {r}")
            return 2

    to_dir = args.to.expanduser().resolve()
    # копируем только с сетевых дисков (или все — с --copy); локальные — на месте
    to_copy = {r for r in roots if args.copy or is_network(r)}
    if to_copy:
        vol = Path(*to_dir.parts[:3]) if to_dir.parts[1:2] == ("Volumes",) else None
        if vol and not os.path.ismount(vol):
            ui.error(f"диск {vol} не подключён (папка назначения: {to_dir}, другая — через --to)")
            return 2
        ui.info(f"Куда копировать: {to_dir}")
    active = [s for s in STEPS if s not in args.skip]
    n = len(active)
    titles = {"flac": "FLAC → WAV", "backups": "Session File Backups"}
    errors = 0
    try:
        for src in roots:
            if src in to_copy and not share_alive(ui, src):  # до любого обхода папки на шаре
                errors += 1
                continue
            src_sessions = find_sessions([src]) if args.only_source else []
            if src in src_sessions:
                ui.warn(f"{src.name}: это сессия, а с --only-source сессии пропускаются")
                continue
            if src in to_copy:
                ui.header("Копирование:", src.name + (" (без сессий)" if args.only_source else ""))
                ui.debug(f"  {src}")
                local = step_copy(ui, src, to_dir, args.dry_run,
                                  "  Копирование на локальный диск", src_sessions)
                # rsync оставляет пустые папки-родители исключённых сессий — убираем
                for sess in src_sessions if local and not args.dry_run else []:
                    d = local / sess.relative_to(src).parent
                    while d != local and d.is_relative_to(local):
                        try:
                            d.rmdir()
                        except OSError:
                            break
                        d = d.parent
                if local is None:
                    errors += 1
                    continue
            else:
                ui.header("На месте:", src.name)
                ui.info(f"  локальный диск — распаковка без копирования: {src}")
                local = src
            # в пробном прогоне копии нет — план показываем по исходной папке
            work = src if args.dry_run else local
            # какие WAV появятся распаковкой (тех, что уже были, не касается): бэкап потом
            # не будет их заново сжимать и отправлять, если они не изменятся
            made = [] if args.dry_run else [
                p.with_suffix(".wav") for p in work.rglob("*")
                if p.suffix.lower() in (".flac", PTREF_EXT) and not p.name.startswith("._")
                and not p.with_suffix(".wav").exists()]
            # сначала файлы по ссылкам на дубликаты: оригиналы ещё во FLAC
            errors += step_refs(ui, work, src if local != src else None, args.dry_run,
                                args.source_dups, args.only_source)
            sessions = find_sessions([work])
            if not args.only_source:
                ui.info(f"Найдено сессий: {len(sessions)}")
            for k, sess in enumerate([] if args.only_source else sessions, 1):
                ui.header(f"Сессия {k}/{len(sessions)}:", sess.name)
                ui.debug(f"  {sess}")
                for i, step in enumerate(active, 1):
                    title = f"  [{i}/{n}] {titles[step]}"
                    if step == "flac":
                        errors += step_decode(ui, sess, max(1, args.jobs), args.dry_run, title)
                    elif step == "backups":
                        errors += step_backups(ui, sess, args.dry_run, title)
            # FLAC в папках проекта вне сессий (Source и т.п.)
            # FLAC от бэкапа вне сессий: Source и т.п., в том числе если
            # восстанавливается папка внутри Source (тогда самой «Source» в копии нет)
            extra = loose_flacs(work, sessions) if "flac" in active else []
            if extra:
                ui.header("Прочие файлы:", work.name)
                errors += step_decode(ui, work, max(1, args.jobs), args.dry_run,
                                      f"  {titles['flac']}", files=extra)
            if not args.only_source:
                if "flac" in active:  # после распаковки сессий: их WAV уже на месте
                    e, got = step_audio(ui, work, src, max(1, args.jobs), args.dry_run)
                    errors += e
                    made += got
                errors += step_video(ui, work, src, args.dry_run, args.video)
            # рабочие копии видео — последними, когда исходное видео уже на месте
            errors += step_workcopy(ui, work, src if local != src else None, args.dry_run,
                                    args.workcopy)
            if not args.dry_run:
                write_restored(work, src if local != src else None, made)
    except KeyboardInterrupt:
        kill_all()
        if ui.active:
            ui.active.close("прервано")
        ui.error("прервано пользователем")
        return 130

    ui.info("Готово." if not errors else ui.red(f"Завершено с ошибками: {errors}"))
    return 0 if not errors else 1


if __name__ == "__main__":
    run_main(main)
