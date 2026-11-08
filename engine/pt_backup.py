#!/usr/bin/env python3
"""
pt_backup — бэкап сессий Pro Tools на SMB-сервер.

Шаги:
  1. WAV -> FLAC (flac -8 --keep-foreign-metadata --delete-input-file) в "Audio Files"
     и "Bounced Files", а также во всех папках проекта вне сессий (Source, PM, ME, …)
  2. Удаление WaveCache.wfm в корне каждой сессии
  3. Архивация "Session File Backups" в zip и удаление папки после проверки архива
  4. rsync -avzP на SMB-шару (с проверкой, монтированием и переподключением).
     Если папка уже есть на сервере:
       - аудио копируется, только если его там нет (WAV и FLAC с одним именем — одно);
       - .ptx/.ptf/.pts сравниваются побайтово; при отличии старая версия на сервере
         переименовывается в "Имя_ГГГГ-ММ-ДД_ЧЧ-ММ-СС.ptx", новая копируется;
       - остальные файлы — по размеру и дате.
  5. (--delete-source) сверка копии на сервере и удаление локальных файлов

--daily: оригиналы не меняются. Сначала каждая сессия сверяется с сервером: если
список аудио тот же (WAV/FLAC с одним именем — одно) и .ptx/.ptf/.pts совпадают
по SHA-256, сессия пропускается целиком. Всё, что нужно отправить, клонируется во временную
папку (.pt_backup_temp рядом с проектом; на APFS клон мгновенный и не занимает место),
шаги 1–4 выполняются над ней, после чего она удаляется.

Примеры:
  pt_backup.py ~/Projects/MyFilm                # папка проекта с несколькими сессиями
  pt_backup.py ~/Sessions/Episode01             # одна сессия
  pt_backup.py -n ~/Projects/MyFilm             # сухой прогон, ничего не меняет
  pt_backup.py -v --dest "/Volumes/Storage A/Other" ~/Projects/MyFilm
  pt_backup.py --delete-source ~/Projects/MyFilm           # бэкап и удаление локальной копии
  pt_backup.py --delete-source --verify checksum -y ~/Projects/MyFilm
  pt_backup.py --daily ~/Projects/MyFilm                   # ежедневный бэкап, локально ничего не меняется

Пароль SMB берётся из связки ключей macOS (сохраняется один раз при подключении
через Finder); при необходимости — переменная окружения PT_BACKUP_SMB_PASS.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import shlex
import errno
import fcntl
import hashlib
import os
import re
import shutil
import select
import signal
import struct
import socket
import subprocess
import tempfile
import sys
import threading
import queue
import time
import traceback
import unicodedata
import tarfile
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote

# ---------------------------------------------------------------- настройки --

SMB_HOST = "192.168.7.186"
SMB_USER = "qarmaa"
SMB_PASS = os.environ.get("PT_BACKUP_SMB_PASS", "")  # пусто — пароль из связки ключей
DEFAULT_DEST = "/Volumes/Storage A/Sessions_2/"

SESSION_EXTS = {".ptx", ".ptf", ".pts"}
AUDIO_DIR = "Audio Files"
# папки сессии, где WAV сжимаются во FLAC (и распаковываются обратно в pt_restore)
FLAC_DIRS = (AUDIO_DIR, "Bounced Files")
WAVECACHE = "WaveCache.wfm"
BACKUPS_DIR = "Session File Backups"
FLAC_FLAGS = ["-8", "--keep-foreign-metadata", "--delete-input-file"]
RSYNC_FLAGS = ["-avzP"]
AUDIO_EXTS = {".wav", ".flac", ".aif", ".aiff", ".mp3", ".m4a", ".caf", ".bwf", ".ogg"}
PARTIAL_DIR = ".rsync-partial"  # недокачанные файлы не лежат под своим именем
STAGING_DIR = ".pt_backup_temp"
# SMB хранит mtime с другой точностью, чем APFS; без допуска rsync будет
# иногда считать неизменённые файлы изменёнными и копировать их заново
MTIME_WINDOW = 2

STEPS = ("flac", "wavecache", "backups", "rsync")


# ------------------------------------------------------------------- вывод --

_lane = threading.local()  # поток конвейера: "local" (сжатие) / "upload" (отправка) — для окна PT Backup


class UI:
    """Вывод сообщений и прогрессбаров.

    В терминале бар перерисовывается на месте через \\r. Если stdout не TTY
    (Console.app, launchd, лог-файл, pipe) — печатаются отдельные строки
    с шагом в 10%, т.к. \\r и ESC-последовательности там не работают.
    """

    current: UI | None = None  # для записи итога/трейсбека в run_main

    def __init__(self, verbose: bool, plain: bool, log_path: Path | None = None,
                 gui: bool = False):
        self.verbose = verbose
        self.gui = gui  # события строками JSON для окна PT Backup (gui/PTBackup)
        self.tty = sys.stdout.isatty() and not plain and not gui
        # цвет только в терминале; NO_COLOR — общепринятый способ отключить
        self.color = self.tty and not os.environ.get("NO_COLOR")
        self.lock = threading.RLock()
        self.bars: list[Bar] = []  # открытые бары; у конвейера их два одновременно
        self.counts = {"warn": 0, "error": 0}  # для истории запусков
        self.stats: dict = {"files": 0, "bytes": 0}  # отправлено за запуск (+ размеры папок, см. backup)
        self.logf = open_log(log_path)
        # где в логе начинается этот запуск — окно PT Backup показывает лог конкретного запуска
        self.log_at = (self.logf.name, self.logf.tell()) if self.logf else None
        UI.current = self
        argv = list(sys.argv)
        for i, a in enumerate(argv[:-1]):  # пароль в лог не пишем
            if a == "--password":
                argv[i + 1] = "***"
        self.log("=" * 12 + f" запуск: {' '.join(map(shlex.quote, argv))}")

    def log(self, msg: str):
        """Строка в лог-файл с датой и временем (без цветовых кодов)."""
        if not self.logf:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        text = ANSI_RE.sub("", msg)
        with self.lock:
            for line in text.splitlines() or [""]:
                self.logf.write(f"[{ts}] {line}\n")
            self.logf.flush()

    @property
    def active(self) -> Bar | None:
        """Последний открытый бар этого потока."""
        me = threading.get_ident()
        return next((b for b in reversed(self.bars) if b.owner == me), None)

    def redraw(self):
        """Строка прогресса в терминале: один бар целиком, несколько — коротко через «│»."""
        cols = shutil.get_terminal_size((80, 20)).columns
        line = self.bars[0].line(cols) if len(self.bars) == 1 else " │ ".join(b.short() for b in self.bars)
        sys.stdout.write("\r\x1b[K" + line[: cols - 1])
        sys.stdout.flush()

    def emit(self, event: dict):
        with self.lock:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _write(self, msg: str, stream=None, log_msg: str | None = None, level: str = "info"):
        stream = stream or sys.stdout
        self.log(msg if log_msg is None else log_msg)
        if self.gui:
            if msg.strip():
                self.emit({"t": "msg", "level": level, "text": ANSI_RE.sub("", msg).strip()})
            return
        with self.lock:
            if self.tty and self.bars:
                sys.stdout.write("\r\x1b[K")
            stream.write(msg + "\n")
            stream.flush()
            if self.tty and self.bars:
                self.redraw()

    def info(self, msg: str):
        self._write(msg)

    def header(self, label: str, name: str):
        """Заметный заголовок того, что сейчас обрабатывается (сессия и т.п.)."""
        plain = f"==== {label} {name} ===="
        if self.gui:
            self.log(plain)
            self.emit({"t": "header", "label": label.rstrip(":"), "name": name,
                       "lane": getattr(_lane, "name", "")})
            return
        if self.color:
            screen = f"\x1b[1;36m▶ {label}\x1b[0m \x1b[1;7m {name} \x1b[0m"
        elif self.tty:
            screen = f"▶ {label} {name}"
        else:
            screen = plain
        self._write("", log_msg="")
        self._write(screen, log_msg=plain)

    def debug(self, msg: str):
        if self.verbose:
            self._write("  " + msg, level="debug")

    def warn(self, msg: str):
        self.counts["warn"] += 1
        self._write(self.yellow("! " + msg), sys.stderr if not self.tty else sys.stdout,
                    level="warn")

    def error(self, msg: str):
        self.counts["error"] += 1
        self._write(self.red("✗ " + msg), sys.stderr if not self.tty else sys.stdout,
                    level="error")

    def red(self, text: str) -> str:
        return f"\x1b[31m{text}\x1b[0m" if self.color else text

    def yellow(self, text: str) -> str:
        return f"\x1b[33m{text}\x1b[0m" if self.color else text

    def bar(self, title: str, total: float, unit: str = "count") -> Bar:
        return Bar(self, title, total, unit)


BAD_STATUS = ("ошиб", "расхожд", "прервано")  # статусы шага, выделяемые красным
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\a]*\a")
LOG_MAX = 10 << 20  # при превышении старый лог уходит в .log.1


def default_log(prog: str) -> Path:
    return Path.home() / "Library" / "Logs" / f"{prog}.log"


def open_log(path: Path | None):
    """Открывает лог на дозапись. Если stdout уже перенаправлен в этот же файл
    (launchd StandardOutPath, >> в терминале), второй раз не пишем."""
    if not path:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX:
            path.replace(path.with_name(path.name + ".1"))
        if path.exists():
            out, log = os.fstat(sys.stdout.fileno()), path.stat()
            if (out.st_dev, out.st_ino) == (log.st_dev, log.st_ino):
                return None
        return open(path, "a", encoding="utf-8")
    except (OSError, ValueError):
        return None


def run_main(main):
    """Запуск main() с записью в лог итога и необработанных ошибок."""
    try:
        rc = main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        rc = 130
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        if UI.current:
            UI.current.log(tb)
        rc = 1
    if UI.current:
        UI.current.log("=" * 12 + f" завершено, код {rc}")
    sys.exit(rc)


def fmt_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


class Bar:
    def __init__(self, ui: UI, title: str, total: float, unit: str):
        self.ui = ui
        self.title = title
        self.total = max(total, 0)
        self.unit = unit  # "count" | "bytes" | "pct"
        self.value = 0.0
        self._last_draw = 0.0
        self._last_step = -1
        self._log_step = -1
        self.rate = ""  # скорость передачи (rsync), показывается после объёма
        self.owner = threading.get_ident()
        self.lane = getattr(_lane, "name", "")
        with ui.lock:
            ui.bars.append(self)
        self.draw(force=True)

    @property
    def pct(self) -> int:
        if self.total <= 0:
            return 100
        return max(0, min(100, int(self.value * 100 / self.total)))

    def _counts(self) -> str:
        if self.unit == "bytes":
            done = f"{fmt_bytes(self.value)}/{fmt_bytes(self.total)}"
            return f"{done}, {self.rate}" if self.rate else done
        if self.unit == "count":
            return f"{int(self.value)}/{int(self.total)}"
        return ""

    def advance(self, n: float = 1):
        with self.ui.lock:
            self.value += n
            self.draw()

    def set(self, value: float):
        with self.ui.lock:
            self.value = value
            self.draw()

    def _log_progress(self):
        step = self.pct // 10
        if step > self._log_step:
            self._log_step = step
            counts = self._counts()
            self.ui.log(f"{self.title}: {self.pct}%" + (f" ({counts})" if counts else ""))

    def _event(self, **extra):
        self.ui.emit({"t": "bar", "title": self.title.strip(), "value": self.value,
                      "total": self.total, "unit": self.unit, "pct": self.pct,
                      "rate": self.rate, "lane": self.lane, **extra})

    def line(self, cols: int) -> str:
        pct, counts = self.pct, self._counts()
        tail = f" {pct:3d}%" + (f"  {counts}" if counts else "")
        width = max(10, min(40, cols - len(self.title) - len(tail) - 3))
        filled = int(width * pct / 100)
        return f"{self.title} {'█' * filled}{'░' * (width - filled)}{tail}"

    def short(self) -> str:
        """Для строки с двумя барами: «FLAC Made in Abyss Aw… 42%» — длинные имена сессий не влезают."""
        step, _, name = re.sub(r"^\[\d+/\d+\] ", "", self.title.strip()).partition(" · ")
        step = {"WAV → FLAC": "FLAC", "rsync → сервер": "→ сервер"}.get(step, step)
        name, colon, sub = name.partition(": ")  # «S1: сравнение» — подшаг не должен уйти под обрезку
        name = (name if len(name) <= 22 else name[:21] + "…") + (f" ({sub})" if colon else "")
        return f"{step} {name} {self.pct}%".replace("  ", " ") + (f" {self.rate}" if self.rate else "")

    def draw(self, force: bool = False):
        with self.ui.lock:
            pct = self.pct
            self._log_progress()
            if self.ui.gui:
                now = time.monotonic()
                if force or now - self._last_draw >= 0.2:
                    self._last_draw = now
                    self._event()
                return
            if self.ui.tty:
                now = time.monotonic()
                if not force and now - self._last_draw < 0.1:
                    return
                self._last_draw = now
                self.ui.redraw()
            else:
                step = pct // 10
                if step > self._last_step or force and self._last_step < 0:
                    self._last_step = step
                    counts = self._counts()
                    print(f"{self.title}: {pct}%" + (f" ({counts})" if counts else ""),
                          flush=True)

    def close(self, status: str = ""):
        with self.ui.lock:
            if status:
                self.ui.log(f"{self.title}: {status}")
            if self in self.ui.bars:
                self.ui.bars.remove(self)
            if self.ui.gui:
                self._event(done=True, status=status)
                return
            if self.ui.tty:
                cols = shutil.get_terminal_size((80, 20)).columns
                if status.startswith(BAD_STATUS):
                    status = self.ui.red(status)
                sys.stdout.write("\r\x1b[K" + self.line(cols)[: cols - 1]
                                 + (f"  {status}" if status else "") + "\n")
                if self.ui.bars:  # второй поток конвейера ещё идёт
                    self.ui.redraw()
                sys.stdout.flush()
            else:
                if self._last_step < 10 and self.pct == 100:
                    self._last_step = -1
                    self.draw()
                if status:
                    print(f"{self.title}: {status}", flush=True)


# ----------------------------------------------------------- подпроцессы --

_procs: set[subprocess.Popen] = set()
_procs_lock = threading.Lock()


def low_prio(cmd: list) -> list:
    """Локальный поток конвейера (flac, zstd) — с пониженным приоритетом: иначе он
    занимает все ядра и rsync в соседнем потоке отправляет втрое медленнее."""
    # не «upload», а не «local»: flac идёт из потоков ThreadPoolExecutor, у них метки потока нет
    return cmd if getattr(_lane, "name", "") == "upload" else ["/usr/bin/nice", "-n", "10", *cmd]


def popen(cmd, **kw) -> subprocess.Popen:
    p = subprocess.Popen(low_prio(cmd), **kw)
    with _procs_lock:
        _procs.add(p)
    return p


def forget(p: subprocess.Popen):
    with _procs_lock:
        _procs.discard(p)


def kill_all():
    with _procs_lock:
        for p in list(_procs):
            try:
                p.terminate()
            except OSError:
                pass


def run_quiet(cmd, timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


# ------------------------------------------------------------ поиск сессий --

def is_session(d: Path) -> bool:
    try:
        return any(f.suffix.lower() in SESSION_EXTS and not f.name.startswith("._")
                   for f in d.iterdir() if f.is_file())
    except OSError:
        return False


def find_sessions(roots: list[Path]) -> list[Path]:
    """Папка-сессия = содержит .ptx/.ptf/.pts. Внутрь сессий не спускаемся."""
    found: list[Path] = []
    for root in roots:
        if is_session(root):
            found.append(root)
            continue
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            here = Path(dirpath)
            if here != root and is_session(here):
                found.append(here)
                dirnames[:] = []
    return found


# ------------------------------------------------------------------- шаги --

def is_wave(p: Path) -> bool:
    """Настоящий WAV (RIFF/RF64/BW64 + WAVE), а не пустой/чужой файл с .wav."""
    try:
        with open(p, "rb") as f:
            h = f.read(12)
    except OSError:
        return False
    return h[:4] in (b"RIFF", b"RF64", b"BW64") and h[8:12] == b"WAVE"


FLAC_MAX_CHUNK = (1 << 24) - 1 - 16  # блок метаданных FLAC минус заголовок «riff» + чанка


# --- WAV с блоками > 16 МБ: звук во FLAC, остальные байты файла — в <имя>.ptchunks
CHUNKS_EXT = ".ptchunks"
CHUNKS_MAGIC = b"PTCHUNKS1"
CHUNKS_HEAD = struct.Struct("<9sQQQ32s")  # magic, начало звука, длина звука, размер WAV, SHA-256


def wave_info(p: Path) -> dict:
    """Разбор WAV для сжатия. problem — почему flac не примет файл (пустой, не WAV,
    обрезан, неподдерживаемый формат), oversize — блоки больше лимита FLAC
    (такой файл сжимается с отдельным файлом блоков). RF64/BW64 (WAV > 4 ГБ):
    64-битные размеры берутся из ds64; ds64_table — в ds64 есть таблица размеров
    блоков, которую flac сохранить не умеет, такой файл тоже идёт через .ptchunks."""
    info = {"problem": None, "oversize": [], "audio_off": 0, "audio_len": 0,
            "ch": 0, "sr": 0, "bits": 0, "align": 0, "size": 0, "legacy_multich": False,
            "ds64_table": False, "rf64": False}
    try:
        size = info["size"] = p.stat().st_size
        if size == 0:
            info["problem"] = "пустой файл (0 байт)"
            return info
        if not is_wave(p):
            info["problem"] = "не WAV по заголовку"
            return info
        with open(p, "rb") as f:
            rf64 = info["rf64"] = f.read(4) != b"RIFF"
            ds_data, ds_table = None, {}
            pos, byte_rate, found_data = 12, 0, False
            while pos + 8 <= size:
                f.seek(pos)
                cid, n = f.read(4), struct.unpack("<I", f.read(4))[0]
                if rf64 and cid == b"ds64":
                    d = f.read(n)
                    _, ds_data, _, count = struct.unpack("<QQQI", d[:28])
                    for k in range(count):
                        e = d[28 + 12 * k:40 + 12 * k]
                        ds_table[e[:4]] = struct.unpack("<Q", e[4:12])[0]
                    info["ds64_table"] = count > 0
                elif rf64 and n == 0xFFFFFFFF:  # настоящий размер — в ds64
                    n = ds_data if cid == b"data" and ds_data is not None else ds_table.get(cid, n)
                if cid == b"fmt ":
                    fmt = f.read(min(n, 40))
                    tag, ch, sr, byte_rate, align, bits = struct.unpack("<HHIIHH", fmt[:16])
                    info.update(ch=ch, sr=sr, bits=bits, align=align,
                                legacy_multich=(tag == 1 and ch > 2))
                    if tag == 0xFFFE and len(fmt) >= 26:  # EXTENSIBLE: реальный тип в SubFormat
                        tag = struct.unpack("<H", fmt[24:26])[0]
                    if tag != 1:  # flac сжимает только целочисленный PCM
                        kind = {3: f"{bits}-bit float", 6: "A-law", 7: "μ-law",
                                2: "ADPCM", 0x11: "IMA ADPCM", 0x55: "MP3"}
                        info["problem"] = f"формат {kind.get(tag, f'0x{tag:04x}')} — flac не поддерживает"
                        return info
                elif cid == b"data":
                    have = size - pos - 8
                    if n > have:
                        info["problem"] = (f"обрезан: {have / byte_rate / 60:.1f} из "
                                           f"{n / byte_rate / 60:.1f} мин" if byte_rate else
                                           f"обрезан: {have:,} из {n:,} байт звука")
                        return info
                    found_data = True
                    info.update(audio_off=pos + 8, audio_len=n)
                elif n > FLAC_MAX_CHUNK:
                    info["oversize"].append((cid.decode("latin-1").strip(), n))
                pos += 8 + n + (n & 1)
            if not found_data:
                info["problem"] = "нет блока со звуком (data)"
            elif (info["oversize"] or info["rf64"]) and (
                    not info["ch"] or info["align"] != info["ch"] * info["bits"] // 8
                    or info["audio_len"] % info["align"]):
                info["problem"] = "нестандартная разметка звука — flac не сожмёт с сохранением заголовка"
    except (OSError, struct.error):
        pass  # не смогли прочитать — пусть flac сам скажет, что не так
    return info


def encode_split(flac: str, w: Path, info: dict) -> tuple[int, str]:
    """Сжатие WAV с блоками > 16 МБ: звук (сырой PCM) -> <имя>.flac, все остальные
    байты файла -> <имя>.ptchunks (+ SHA-256 исходника). WAV удаляется только после
    проверки FLAC; pt_restore собирает исходный файл байт в байт."""
    out, side = w.with_suffix(".flac"), w.with_suffix(CHUNKS_EXT)
    out_tmp, side_tmp = out.with_name(out.name + ".part"), side.with_name(side.name + ".part")
    off, n, size = info["audio_off"], info["audio_len"], info["size"]
    sha = hashlib.sha256()
    try:
        cmd = [flac, "-8", "-f", "-s", "--force-raw-format", "--endian=little",
               "--sign=" + ("unsigned" if info["bits"] <= 8 else "signed"),
               f"--channels={info['ch']}", f"--bps={info['bits']}",
               f"--sample-rate={info['sr']}", "-o", str(out_tmp), "-"]
        p = popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with open(w, "rb") as src:
                head = src.read(off)
                sha.update(head)
                left = n
                while left:
                    chunk = src.read(min(left, 8 << 20))
                    if not chunk:
                        raise OSError("файл закончился раньше звука")
                    sha.update(chunk)
                    p.stdin.write(chunk)
                    left -= len(chunk)
                p.stdin.close()
                tail = src.read()
                sha.update(tail)
            err = p.stderr.read().decode("utf-8", "replace")
            p.wait()
        finally:
            forget(p)
        if p.returncode != 0:
            return p.returncode, err
        t = subprocess.run(low_prio([flac, "-t", "-s", str(out_tmp)]), capture_output=True, text=True)
        if t.returncode != 0:
            return t.returncode, "проверка FLAC: " + t.stderr
        with open(side_tmp, "wb") as fh:
            fh.write(CHUNKS_HEAD.pack(CHUNKS_MAGIC, off, n, size, sha.digest()))
            fh.write(head)
            fh.write(tail)
        st = w.stat()
        for tmp, final in ((out_tmp, out), (side_tmp, side)):
            tmp.rename(final)
            os.utime(final, (st.st_atime, st.st_mtime))
        w.unlink()
        return 0, ""
    except (OSError, BrokenPipeError) as e:
        return 1, str(e)
    finally:
        out_tmp.unlink(missing_ok=True)
        side_tmp.unlink(missing_ok=True)


def loose_wavs(root: Path, sessions: list[Path]) -> list[Path]:
    """Все WAV проекта вне сессий (Source, PM, ME и любые другие папки)."""
    return sorted(d / f for d, files in walk_pruned(root, set(sessions)) for f in files
                  if f.lower().endswith(".wav") and not f.startswith("._"))


def step_flac(ui: UI, sessions: list[Path], jobs: int, dry: bool, title: str,
              dirs: list[Path] | None = None, files: list[Path] | None = None,
              keep=None) -> int:
    """WAV -> FLAC в FLAC_DIRS сессий, в папках dirs или в списке files.
    keep(wav) -> True: не сжимать (не менялся после распаковки, на сервере уже есть)."""
    if dirs is None:
        dirs = [s / sub for s in sessions for sub in FLAC_DIRS]
    wavs: list[Path] = list(files) if files is not None else []
    for audio in dirs if files is None else []:
        if audio.is_dir():
            wavs += sorted(p for p in audio.rglob("*")
                           if p.is_file() and p.suffix.lower() == ".wav"
                           and not p.name.startswith("._"))
    if keep:
        kept = [w for w in wavs if keep(w)]
        if kept:
            wavs = [w for w in wavs if w not in set(kept)]
            ui.info(f"{title}: не сжимаются {len(kept)} WAV — не менялись после распаковки, на сервере уже есть")
    # рядом уже есть свой X.flac (не от этого бэкапа) — flac -f перезаписал бы его: WAV
    # не сжимаем, уходит как есть
    for w in [w for w in wavs if w.with_suffix(".flac").exists()]:
        wavs.remove(w)
        ui.warn(f"{w.name}: рядом уже есть {w.with_suffix('.flac').name} — WAV не сжимается, чтобы его не затереть")
    # пустые, обрезанные и не-WAV файлы flac не примет — оставляем как есть,
    # rsync их скопирует; это предупреждение, а не ошибка (не мешает и --delete-source)
    split: dict[Path, dict] = {}  # WAV с блоками > 16 МБ — сжатие с файлом блоков
    # >2 каналов без WAVE_FORMAT_EXTENSIBLE: раскладка не указана, flac без
    # --channel-map=none откажется; заголовок всё равно восстановится из блоков
    no_map: set[Path] = set()
    for w in list(wavs):
        info = wave_info(w)
        if info["problem"]:
            wavs.remove(w)
            ui.warn(f"{w.name}: {info['problem']}, не сжимается — копируется как есть")
            continue
        # RF64/BW64 (WAV > 4 ГБ) — тоже через .ptchunks: flac не восстанавливает их
        # заголовок байт в байт (а с таблицей в ds64 и не сжимает)
        if info["oversize"] or info["rf64"]:
            split[w] = info
            why = (f"блоки больше 16 МБ ({', '.join(f'{c} {n / 2**20:.0f} МБ' for c, n in info['oversize'])})"
                   if info["oversize"] else "RF64 (WAV больше 4 ГБ)")
            ui.debug(f"{w.name}: {why} — заголовок и блоки отдельно в {CHUNKS_EXT}")
        if info["legacy_multich"]:
            no_map.add(w)
    if not wavs:
        ui.info(f"{title}: нет WAV-файлов")
        return 0
    if dry:
        for w in wavs:
            ui.debug(f"flac {w}")
        ui.info(f"{title}: будет сжато {len(wavs)} WAV "
                f"({fmt_bytes(sum(w.stat().st_size for w in wavs))})")
        return 0

    flac = shutil.which("flac") or "/opt/homebrew/bin/flac"
    sizes = {w: w.stat().st_size for w in wavs}
    bar = ui.bar(title, sum(sizes.values()), "bytes")
    errors = 0
    skipped = 0

    def encode(w: Path):
        if w in split:
            rc, err = encode_split(flac, w, split[w])
            return w, rc, err
        cmd = [flac, *FLAC_FLAGS, *(["--channel-map=none"] if w in no_map else []),
               "-f", "-s", str(w)]
        p = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            _, err = p.communicate()
        finally:
            forget(p)
        return w, p.returncode, err

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = [ex.submit(encode, w) for w in wavs]
        for f in as_completed(futs):
            w, rc, err = f.result()
            if rc == 0:
                ui.debug(f"FLAC ✓ {w}")
            else:
                # WAV не тронут (flac удаляет исходник только при успехе) и уйдёт как есть;
                # недописанный FLAC убираем, иначе на сервер ушли бы оба файла
                out = w.with_suffix(".flac")
                if w.exists() and out.exists():
                    out.unlink()
                skipped += 1
                reason = next((l.split("ERROR", 1)[1].lstrip(" :") for l in err.splitlines()
                               if "ERROR" in l), f"код {rc}")
                ui.warn(f"{w.name}: flac не сжал ({reason}) — копируется как есть")
                ui.debug(err.strip()[-500:])
            bar.advance(sizes[w])
    bar.close("готово" if not skipped else f"готово, не сжато: {skipped}")
    return errors


def step_wavecache(ui: UI, sessions: list[Path], dry: bool) -> int:
    """Удаляет WaveCache.wfm молча (виден только с -v)."""
    for s in sessions:
        t = s / WAVECACHE
        if not t.is_file():
            continue
        if dry:
            ui.debug(f"rm {t}")
            continue
        try:
            t.unlink()
            ui.debug(f"удалён {t}")
        except OSError as e:
            ui.debug(f"не удалось удалить {t}: {e}")
    return 0


# --- Session File Backups: сотни почти одинаковых версий .ptx. С .ptx снимается
# XOR-шифр Pro Tools (симметричный, ключ меняется раз в 4 КБ) и всё пакуется одним
# сплошным zstd-архивом с большим окном — повторы между версиями сжимаются в разы
# лучше, чем zip по файлам. pt_restore возвращает XOR, .ptx получаются байт в байт.

PTBAK_EXT = ".ptbak"
PTBAK_MANIFEST = ".ptbak.json"
ZSTD = shutil.which("zstd") or "/opt/homebrew/bin/zstd"
ZSTD_LEVEL = ["-10", "--long=31"]   # распаковка тоже требует --long=31
_XOR_TABLES: dict[int, bytes] = {}


def ptx_xor_key(xor_type: int, xor_value: int) -> bytes | None:
    if xor_type == 0x01:
        mul, neg = 53, False
    elif xor_type == 0x05:
        mul, neg = 11, True
    else:
        return None
    delta = 0
    for i in range(256):
        if (i * mul) & 0xFF == xor_value:
            delta = (-i) & 0xFF if neg else i
            break
    return bytes((i * delta) & 0xFF for i in range(256))


def ptx_crypt(data: bytes) -> bytes | None:
    """Снять/наложить XOR .ptx (одна и та же операция). None — не похоже на .ptx.
    Первые 0x14 байт не шифруются, ключ задают байты 0x12/0x13 и номер 4К-страницы."""
    if len(data) < 0x14:
        return None
    key = ptx_xor_key(data[0x12], data[0x13])
    if key is None:
        return None
    out = bytearray(data)
    pos, n = 0x14, len(data)
    while pos < n:
        end = min(n, ((pos >> 12) + 1) << 12)
        k = key[(pos >> 12) & 0xFF]
        tbl = _XOR_TABLES.get(k)
        if tbl is None:
            tbl = _XOR_TABLES[k] = bytes(x ^ k for x in range(256))
        out[pos:end] = bytes(data[pos:end]).translate(tbl)
        pos = end
    return bytes(out)


def backup_archives(session: Path) -> list[Path]:
    """Архивы бэкапов сессии: Session File Backups[ <дата>].zip / .ptbak, от старых к новым."""
    arcs = [p for p in session.glob(f"{BACKUPS_DIR}*") if p.is_file()
            and p.suffix.lower() in (".zip", PTBAK_EXT) and not p.name.endswith(".part")]
    return sorted(arcs, key=lambda p: p.stat().st_mtime)


def iter_archive(path: Path):
    """(имя внутри Session File Backups, данные без XOR, xor-флаг, mtime) из старого архива."""
    if path.suffix.lower() == PTBAK_EXT:
        p = popen([ZSTD, "-d", "-q", "--long=31", "-c", str(path)],
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                for ti in tf:
                    if ti.isfile() and ti.name != PTBAK_MANIFEST:
                        name = ti.name.split("/", 1)[1] if "/" in ti.name else ti.name
                        yield name, tf.extractfile(ti).read(), \
                            ti.pax_headers.get("PTBAK.xor") == "1", ti.mtime
            err = p.stderr.read().decode("utf-8", "replace")
            if p.wait() != 0:
                raise RuntimeError(f"{path.name}: zstd: {err.strip()[-200:]}")
        finally:
            forget(p)
    else:
        with zipfile.ZipFile(path) as zf:
            for zi in zf.infolist():
                if zi.is_dir():
                    continue
                name = zi.filename.split("/", 1)[1] if zi.filename.startswith(BACKUPS_DIR + "/") \
                    else zi.filename
                data = zf.read(zi)
                plain = ptx_crypt(data) if Path(name).suffix.lower() in (".ptx", ".ptf") else None
                yield (name, plain, True, time.mktime(zi.date_time + (0, 0, -1))) if plain \
                    is not None else (name, data, False, time.mktime(zi.date_time + (0, 0, -1)))


KEEP_BACKUPS = 30  # бэкапов Pro Tools на каждое имя сессии: старше — в архив не идут


def backup_session_name(name: str) -> str:
    """«X.bak.012.ptx» (и «X.bak.012_<дата>.ptx» после переименования) -> «X»."""
    base = name.rsplit("/", 1)[-1]
    return base.split(".bak.")[0] if ".bak." in base else base


def newest_backups(entries: list[tuple[str, float]]) -> set[str]:
    """Имена KEEP_BACKUPS самых свежих (по дате) бэкапов каждого имени сессии."""
    keep = KEEP_BACKUPS
    by: dict[str, list[tuple[float, str]]] = {}
    for name, mtime in entries:
        by.setdefault(backup_session_name(name), []).append((mtime, name))
    return {n for v in by.values() for _, n in sorted(v, reverse=True)[:keep]}


def archive_list(path: Path) -> list[tuple[str, float]]:
    """(имя, дата) файлов архива без чтения их содержимого в память."""
    if path.suffix.lower() == PTBAK_EXT:
        p = popen([ZSTD, "-d", "-q", "--long=31", "-c", str(path)],
                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                return [(ti.name.split("/", 1)[1] if "/" in ti.name else ti.name, ti.mtime)
                        for ti in tf if ti.isfile() and ti.name != PTBAK_MANIFEST]
        finally:
            p.wait()
            forget(p)
    with zipfile.ZipFile(path) as zf:
        return [(zi.filename.split("/", 1)[1] if zi.filename.startswith(BACKUPS_DIR + "/") else zi.filename,
                 time.mktime(zi.date_time + (0, 0, -1))) for zi in zf.infolist() if not zi.is_dir()]


def archive_bytes(path: Path) -> int:
    if path.suffix.lower() == PTBAK_EXT:
        p = subprocess.Popen([ZSTD, "-d", "-q", "--long=31", "-c", str(path)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                ti = tf.next()
                if ti is not None and ti.name == PTBAK_MANIFEST:
                    return json.loads(tf.extractfile(ti).read()).get("bytes", 0)
        except (tarfile.TarError, ValueError):
            return 0
        finally:
            p.kill()
            p.wait()
        return 0
    try:
        return sum(i.file_size for i in zipfile.ZipFile(path).infolist())
    except zipfile.BadZipFile:
        return 0


def pack_ptbak(d: Path, files: list[Path], old: list[Path], tmp: Path, bar: Bar,
               old_bytes: dict[Path, int]) -> dict:
    """Один сплошной архив: бэкапы из папки d + содержимое старых архивов сессии.
    tar (PAX) -> zstd; с .ptx/.ptf снят XOR (pax PTBAK.xor=1). Одноимённые файлы
    с разным содержимым из старых архивов сохраняются с датой в имени.
    Возвращает {имя в архиве: размер} для проверки."""
    want: dict[str, int] = {}
    sizes: dict[str, int] = {}
    total = sum(p.stat().st_size for p in files) + sum(old_bytes[a] for a in old)
    p = popen([ZSTD, "-q", *ZSTD_LEVEL, "-T0", "-f", "-o", str(tmp), "-"],
              stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=p.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tf:
            def add(name: str, data: bytes, xor: bool, mtime: float, orig_size: int):
                ti = tarfile.TarInfo(f"{BACKUPS_DIR}/{name}")
                ti.size, ti.mtime, ti.mode = len(data), mtime, 0o644
                if xor:
                    ti.pax_headers = {"PTBAK.xor": "1"}
                tf.addfile(ti, io.BytesIO(data))
                want[ti.name] = len(data)
                sizes[name] = orig_size
                bar.advance(orig_size)

            manifest = json.dumps({"v": 1, "bytes": total}).encode()
            ti = tarfile.TarInfo(PTBAK_MANIFEST)
            ti.size = len(manifest)
            tf.addfile(ti, io.BytesIO(manifest))
            seen: dict[str, bytes] = {}  # имя -> данные без XOR (для сравнения со старыми)
            # по каждому имени сессии — только KEEP_BACKUPS самых свежих (локальные + из старых архивов)
            keep = newest_backups([(f.relative_to(d).as_posix(), f.stat().st_mtime) for f in files]
                                  + [e for a in old for e in archive_list(a)])
            for f in files:
                if f.relative_to(d).as_posix() not in keep:
                    bar.advance(f.stat().st_size)
                    continue
                data, st = f.read_bytes(), f.stat()
                plain = ptx_crypt(data) if f.suffix.lower() in (".ptx", ".ptf") else None
                name = f.relative_to(d).as_posix()
                add(name, plain if plain is not None else data, plain is not None, st.st_mtime,
                    st.st_size)
                seen[name] = hashlib.sha256(plain if plain is not None else data).digest()
            for a in old:  # старые архивы этой сессии: всё, чего ещё нет
                for name, data, xor, mtime in iter_archive(a):
                    if name not in keep:
                        bar.advance(len(data))
                        continue
                    h = hashlib.sha256(data).digest()
                    if seen.get(name) == h:
                        bar.advance(len(data))
                        continue
                    if name in seen:  # то же имя, другое содержимое (Pro Tools начал нумерацию заново)
                        stem, suf = os.path.splitext(name)
                        ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(mtime))
                        new, i = f"{stem}_{ts}{suf}", 2
                        while new in seen and seen[new] != h:
                            new, i = f"{stem}_{ts}_{i}{suf}", i + 1
                        if new in seen:
                            bar.advance(len(data))
                            continue
                        name = new
                    add(name, data, xor, mtime, len(data))
                    seen[name] = h
        p.stdin.close()
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise RuntimeError(f"zstd: {err.strip()[-300:]}")
    finally:
        forget(p)
    return want


def check_ptbak(path: Path, want: dict):
    """Тестовая распаковка: zstd проверяет контрольную сумму архива,
    плюс сверяются имена и размеры файлов."""
    p = popen([ZSTD, "-d", "-q", "--long=31", "-c", str(path)],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    got = {}
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            for ti in tf:
                if ti.isfile() and ti.name != PTBAK_MANIFEST:
                    got[ti.name] = ti.size
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise RuntimeError(f"проверка архива: {err.strip()[-300:]}")
    finally:
        forget(p)
    if got != want:
        raise RuntimeError(f"в архиве {len(got)} файлов из {len(want)} или не те размеры")


def archive_covers(archive: Path, d: Path, files: list[Path]) -> bool:
    """Все бэкапы из папки d уже есть в архиве с тем же содержимым (например, папку только
    что развернул pt_restore) — новый архив ничего бы не добавил."""
    want = {}
    for f in files:
        data = f.read_bytes()
        plain = ptx_crypt(data) if f.suffix.lower() in (".ptx", ".ptf") else None
        want[f.relative_to(d).as_posix()] = hashlib.sha256(plain if plain is not None else data).digest()
    listed = []
    for name, data, _, mtime in iter_archive(archive):
        listed.append((name, mtime))
        if want.get(name) == hashlib.sha256(data).digest():
            del want[name]
    # недостающие локальные, которые в архив всё равно не попали бы (старше KEEP_BACKUPS), не в счёт
    keep = newest_backups(listed + [(f.relative_to(d).as_posix(), f.stat().st_mtime) for f in files])
    return not (set(want) & keep)


def step_backups(ui: UI, sessions: list[Path], dry: bool, title: str,
                 server_dirs: dict | None = None, packed: set | None = None) -> int:
    """Session File Backups -> один «Session File Backups.ptbak» на сессию: новые
    бэкапы из папки + содержимое прежних архивов (zip/ptbak) — локальных и тех, что
    уже лежат на сервере (server_dirs: сессия -> её папка на сервере), чтобы при
    замене не потерять версии, которых локально уже нет. Прежние локальные архивы
    удаляются здесь, серверные — после успешной отправки (prune_server_archives), и только
    у сессий, попавших в packed: их архивы целиком вошли в новый .ptbak в этом запуске."""
    use_zstd = os.path.exists(ZSTD)
    jobs = []
    for s in sessions:
        d = s / BACKUPS_DIR
        old = backup_archives(s)
        srv = (server_dirs or {}).get(s)
        if use_zstd and srv is not None:
            local = {(a.name, a.stat().st_size) for a in old}
            try:
                old += [a for a in backup_archives(srv) if (a.name, a.stat().st_size) not in local]
            except OSError:
                pass
        canon = s / f"{BACKUPS_DIR}{PTBAK_EXT}"
        if d.is_dir() or (use_zstd and old and [a for a in old if a != canon]):
            files = sorted(p for p in d.rglob("*") if p.is_file() and p.name not in JUNK) \
                if d.is_dir() else []
            jobs.append((s, d, files, old if use_zstd else []))
    if not jobs:
        ui.info(f"{title}: не найдено")
        return 0
    if dry:
        for s, d, files, old in jobs:
            ui.debug(f"{s.name}: {len(files)} бэкапов + {len(old)} прежних архивов -> "
                     f"{BACKUPS_DIR}{PTBAK_EXT if use_zstd else '.zip'}")
        ui.info(f"{title}: будет упаковано {len(jobs)} сессий")
        return 0
    if not use_zstd:
        ui.warn(f"zstd не найден ({ZSTD}) — бэкапы сессий упаковываются в zip (brew install zstd)")

    old_bytes = {a: archive_bytes(a) for *_, old in jobs for a in old}
    total = sum(p.stat().st_size for _, _, files, _ in jobs for p in files) + sum(old_bytes.values())
    bar = ui.bar(title, total, "bytes")
    errors = packed_bytes = 0
    for s, d, files, old in jobs:
        dst = s / f"{BACKUPS_DIR}{PTBAK_EXT}" if use_zstd else \
            (s / f"{BACKUPS_DIR}.zip" if not (s / f"{BACKUPS_DIR}.zip").exists()
             else s / f"{BACKUPS_DIR} {time.strftime('%Y%m%d-%H%M%S')}.zip")
        tmp = dst.with_name(dst.name + ".part")
        try:
            srv_arch = old[0] if len(old) == 1 and old[0].parent != s else None
            if use_zstd and srv_arch and srv_arch.name == dst.name and archive_covers(srv_arch, d, files):
                # архив на сервере уже содержит всё это: берём его как есть — та же дата и
                # размер, rsync не отправит его заново (переупаковка дала бы другие байты)
                shutil.copy2(srv_arch, tmp)
                tmp.replace(dst)
                packed_bytes += dst.stat().st_size
                bar.advance(sum(p.stat().st_size for p in files) + old_bytes.get(srv_arch, 0))
                if d.is_dir():
                    shutil.rmtree(d)
                ui.debug(f"{s.name}: бэкапы уже в архиве на сервере — взят оттуда")
                continue
            if use_zstd:
                want = pack_ptbak(d, files, old, tmp, bar, old_bytes)
                check_ptbak(tmp, want)
            else:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                    if not files:
                        zf.writestr(BACKUPS_DIR + "/", "")
                    for p in files:
                        zf.write(p, Path(BACKUPS_DIR) / p.relative_to(d))
                        bar.advance(p.stat().st_size)
                with zipfile.ZipFile(tmp) as zf:  # проверка архива перед удалением
                    bad = zf.testzip()
                    names = {n for n in zf.namelist() if not n.endswith("/")}
                if bad is not None:
                    raise RuntimeError(f"битый файл в архиве: {bad}")
                if len(names) != len(files):
                    raise RuntimeError(f"в архиве {len(names)} файлов из {len(files)}")
            newest = max([p.stat().st_mtime for p in files] + [a.stat().st_mtime for a in old],
                         default=None)
            tmp.replace(dst)
            packed_bytes += dst.stat().st_size
            # дата архива = дата самого нового бэкапа: при --daily неизменённые
            # бэкапы дают архив с той же датой, и rsync его не передаёт повторно
            if newest:
                os.utime(dst, (newest, newest))
            for a in old:  # прежние локальные архивы вошли в новый (серверные — после отправки)
                if a != dst and a.parent == s:
                    a.unlink()
            if d.is_dir():
                shutil.rmtree(d)
            if use_zstd and packed is not None:
                packed.add(s)
            ui.debug(f"{s.name}: {len(files)} бэкапов + {len(old)} прежних архивов -> {dst.name}")
        except Exception as e:  # noqa: BLE001
            errors += 1
            ui.error(f"{d}: {e} (папка и прежние архивы не тронуты)")
            tmp.unlink(missing_ok=True)
    bar.close(f"готово: {fmt_bytes(total)} → {fmt_bytes(packed_bytes)}" if not errors
              else f"ошибок: {errors}")
    return errors


def prune_server_archives(ui: UI, session: Path, server_dir: Path):
    """На сервере у сессии остаётся один архив бэкапов — тот, что только что ушёл."""
    canon = session / f"{BACKUPS_DIR}{PTBAK_EXT}"
    if not canon.exists() or not (server_dir / canon.name).exists():
        return
    try:
        old = backup_archives(server_dir)
    except OSError as e:
        ui.warn(f"{server_dir.name}: не удалось прочитать архивы бэкапов на сервере: {e}")
        return
    for f in old:
        if f.name != canon.name:
            try:
                f.unlink()
                ui.debug(f"на сервере удалён прежний архив бэкапов: {f.name}")
            except OSError as e:
                ui.warn(f"{f.name}: не удалось удалить на сервере: {e}")


# --------------------------------------------------------------------- SMB --

class SMB:
    """Проверка доступности сервера, монтирование и переподключение шары.

    dest вида /Volumes/<share>/<путь>. Если шара уже смонтирована в другом
    месте (например "/Volumes/Storage A-1"), используется фактическая точка.
    """

    def __init__(self, ui: UI, host: str, user: str, password: str, share: str,
                 dest_rel: str, retries: int, delay: float):
        self.ui = ui
        self.host = host
        self.user = user
        self.password = password
        self.share = share
        self.dest_rel = dest_rel
        self.retries = retries
        self.delay = delay

    # --- проверки
    def server_up(self, timeout: float = 3) -> bool:
        try:
            with socket.create_connection((self.host, 445), timeout=timeout):
                return True
        except OSError:
            pass
        # «Локальная сеть» в macOS может не пускать python, запущенный из приложения
        # (окно PT Backup, терминал Claude), а системный nc пускает
        r = run_quiet(["/usr/bin/nc", "-z", "-G", str(int(timeout)), self.host, "445"], timeout + 5)
        return bool(r and r.returncode == 0)

    def mount_point(self) -> Path | None:
        """Точка монтирования шары; при нескольких — /Volumes/<share> в приоритете."""
        r = run_quiet(["/sbin/mount"], 10)
        if not r:
            return None
        points = []
        for line in r.stdout.splitlines():
            m = re.match(r"^//(?:[^@]+@)?[^/]+/(.+?) on (.+) \(smbfs", line)
            if m and unquote(m.group(1)).lower() == self.share.lower():
                points.append(Path(m.group(2)))
        if not points:
            return None
        preferred = Path("/Volumes") / self.share
        return preferred if preferred in points else points[0]

    def dest(self) -> Path | None:
        mp = self.mount_point()
        return mp / self.dest_rel if mp else None

    def healthy(self) -> bool:
        """Шара смонтирована, отвечает и доступна на запись (с таймаутами)."""
        dest = self.dest()
        if not dest:
            return False
        if run_quiet(["/bin/mkdir", "-p", str(dest)], 20) is None:
            return False
        probe = dest / f".pt_backup_probe_{os.getpid()}"
        r = run_quiet(["/usr/bin/touch", str(probe)], 20)
        if not r or r.returncode != 0:
            return False
        run_quiet(["/bin/rm", "-f", str(probe)], 20)
        return True

    # --- действия
    def _mount(self) -> bool:
        # без пароля в URL macOS берёт его из связки ключей (и он не виден в `ps`)
        auth = f":{quote(self.password, safe='')}" if self.password else ""
        url = (f"smb://{quote(self.user, safe='')}{auth}"
               f"@{self.host}/{quote(self.share)}")
        self.ui.debug(f"монтирование smb://{self.user}@{self.host}/{self.share}")
        r = run_quiet(["/usr/bin/osascript", "-e", f'mount volume "{url}"'], 60)
        if r and r.returncode == 0 and self.mount_point():
            return True
        self.ui.debug(f"osascript: {(r.stderr.strip() if r else 'таймаут')}")
        # запасной путь без GUI-сессии (launchd/ssh): mount_smbfs в домашнюю папку
        mp = Path.home() / ".pt_backup_mnt" / self.share
        mp.mkdir(parents=True, exist_ok=True)
        r = run_quiet(["/sbin/mount_smbfs", "-N", url.replace("smb:", "", 1), str(mp)], 60)
        if r and r.returncode == 0:
            return True
        self.ui.debug(f"mount_smbfs: {(r.stderr.strip() if r else 'таймаут')}")
        return False

    def _unmount(self, mp: Path):
        self.ui.debug(f"размонтирование зависшей шары {mp}")
        run_quiet(["/usr/sbin/diskutil", "unmount", "force", str(mp)], 30)

    def ensure(self) -> Path:
        for attempt in range(1, self.retries + 1):
            if not self.server_up():
                self.ui.warn(f"сервер {self.host} недоступен "
                             f"(попытка {attempt}/{self.retries})")
            else:
                if self.healthy():
                    return self.dest()  # type: ignore[return-value]
                mp = self.mount_point()
                if mp:
                    self._unmount(mp)
                if self._mount() and self.healthy():
                    self.ui.debug(f"подключено: {self.mount_point()}")
                    return self.dest()  # type: ignore[return-value]
                self.ui.warn(f"не удалось подключить //{self.host}/{self.share} "
                             f"(попытка {attempt}/{self.retries})")
            if attempt < self.retries:
                time.sleep(min(self.delay * attempt, 60))
        raise ConnectionError(f"нет доступа к //{self.host}/{self.share}")


NETWORK_FS = {"smbfs", "afpfs", "nfs", "webdav", "cifs"}


def mount_of(path: Path) -> tuple[str, str]:
    """(точка монтирования, тип ФС) для path — по самой длинной точке из `mount`,
    к которой относится путь (без обращения к самому пути: шара может висеть)."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "", ""
    best, kind = "", ""
    p = str(path)
    for line in out.splitlines():
        head, sep, tail = line.rpartition(" (")
        if not sep or " on " not in head:
            continue
        mp = head.split(" on ", 1)[1]
        if (p == mp or p.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
            best, kind = mp, tail.split(",", 1)[0].rstrip(")")
    return best, kind


def is_network(path: Path) -> bool:
    return mount_of(path)[1] in NETWORK_FS


def local_volume(dest: Path) -> bool:
    """Назначение — обычная папка, а не SMB-шара: вне /Volumes или на подключённом
    не-сетевом томе (USB-диск и т.п.). Неподключённый /Volumes/<имя> — шара сервера."""
    if dest.parts[1:2] != ("Volumes",):
        return True
    mp, kind = mount_of(dest)
    return mp == str(Path(*dest.parts[:3])) and kind not in NETWORK_FS


def make_smb(ui: UI, dest: Path, host: str, user: str, password: str, share: str | None,
             retries: int, delay: float) -> SMB | None:
    """SMB для пути вида /Volumes/<share>/<путь>; None (с сообщением), если шару не понять."""
    parts = dest.parts
    share = share or (parts[2] if len(parts) > 2 and parts[1] == "Volumes" else None)
    if not share:
        ui.error("не удалось определить имя шары из --dest, укажите --share или --local")
        return None
    dest_rel = str(Path(*parts[3:])) if parts[1:3] == ("Volumes", share) else ""
    return SMB(ui, host, user, password, share, dest_rel, retries, delay)


# ------------------------------------------------------------------- rsync --

def find_rsync() -> tuple[str, bool]:
    """Путь к rsync и поддержка --info=progress2 (GNU rsync >= 3.1)."""
    for c in ("/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", shutil.which("rsync")):
        if c and os.path.exists(c):
            r = run_quiet([c, "--version"], 10)
            m = re.search(r"version (\d+)\.(\d+)", r.stdout if r else "")
            if m and (int(m[1]), int(m[2])) >= (3, 1):
                return c, True
    return shutil.which("rsync") or "/usr/bin/rsync", False


# --info=progress2: «  1,234,567  45%   82.34MB/s    0:00:12 …» — байты, %, скорость
PROGRESS_RE = re.compile(rb"^\s*([\d,]+)\s+(\d{1,3})%(?:\s+([\d.]+)([kMGT]?)B/s)?")
RATE_MULT = {b"": 1, b"k": 1 << 10, b"M": 1 << 20, b"G": 1 << 30, b"T": 1 << 40}
XFR_RE = re.compile(rb"xfr#(\d+)")


RSYNC_STALLED = -100  # код «передача зависла» (rsync снят сторожем)
STALL_SECONDS = int(os.environ.get("PT_BACKUP_STALL", "180"))


def stall_text() -> str:
    return f"{STALL_SECONDS // 60} мин" if STALL_SECONDS >= 60 else f"{STALL_SECONDS} с"


def run_rsync(ui: UI, bar: Bar, cmd: list[str]) -> tuple[int, str, int]:
    """rsync с разбором прогресса. Сторож: если STALL_SECONDS не передано ни байта
    (например, зависла SMB-шара), rsync снимается и возвращается RSYNC_STALLED."""
    ui.debug(" ".join(cmd))
    # своя группа процессов: rsync запускает несколько процессов, снимать — все
    p = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    err_chunks: list[bytes] = []
    t = threading.Thread(target=lambda: err_chunks.append(p.stderr.read()), daemon=True)
    t.start()
    buf = b""
    transferred = 0
    last_bytes, last_move = -1, time.monotonic()
    stalled = False
    fd = p.stdout.fileno()
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 5)
            if not ready:
                if time.monotonic() - last_move > STALL_SECONDS:
                    stalled = True
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for line in lines:
                m = PROGRESS_RE.match(line)
                if m:
                    done = int(m.group(1).replace(b",", b""))
                    if done != last_bytes:  # прогресс есть
                        last_bytes, last_move = done, time.monotonic()
                    elif time.monotonic() - last_move > STALL_SECONDS:
                        stalled = True
                    if m.group(3):
                        bar.rate = fmt_bytes(float(m.group(3)) * RATE_MULT[m.group(4)]) + "/s"
                    if bar.unit == "bytes":  # передано байт из заранее известного объёма
                        bar.set(min(done, bar.total))
                    else:
                        bar.set(int(m.group(2)))
                    x = XFR_RE.search(line)
                    if x:
                        transferred = int(x.group(1))
                elif line.strip():
                    last_move = time.monotonic()  # список файлов и т.п. — rsync жив
                    ui.debug(line.decode("utf-8", "replace"))
            if stalled:
                break
        if stalled:
            # процесс, висящий в ядре на зависшей шаре, может не умереть сразу —
            # не ждём его бесконечно, после переподключения он завершится сам
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                p.kill()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return RSYNC_STALLED, f"нет передачи данных {stall_text()}", transferred
        p.wait()
    finally:
        forget(p)
    t.join(5)
    return p.returncode, b"".join(err_chunks).decode("utf-8", "replace"), transferred


def name_key(rel) -> str:
    """Ключ для сравнения имён: SMB отдаёт «й», «ё» и т.п. в NFD, а APFS хранит
    их как создали (обычно NFC) — без нормализации одинаковые имена не совпадают."""
    return unicodedata.normalize("NFC", str(rel))


def scan_remote(dest_root: Path, skip: set[str] = frozenset()) -> dict[str, os.stat_result]:
    """Файлы на сервере: относительный путь -> stat. Пусто, если папки ещё нет.
    skip — относительные пути каталогов, в которые не заходить."""
    out: dict[str, os.stat_result] = {}
    if not dest_root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(dest_root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d != PARTIAL_DIR
                       and name_key((here / d).relative_to(dest_root)) not in skip]
        for f in filenames:
            p = Path(dirpath) / f
            out[name_key(p.relative_to(dest_root))] = p.lstat()
    return out


def is_audio(rel: Path) -> bool:
    return rel.suffix.lower() in AUDIO_EXTS


def audio_present(rel: Path, remote: dict) -> bool:
    """Аудио есть на сервере. WAV и FLAC с одним именем считаются одним файлом."""
    names = {name_key(rel)}
    if rel.suffix.lower() in (".wav", ".flac"):
        names |= {name_key(rel.with_suffix(e)) for e in (".wav", ".WAV", ".flac", PTREF_EXT)}
    elif is_audio(rel):
        names.add(name_key(rel.with_suffix(PTREF_EXT)))
    return any(n in remote for n in names)


_md5_cache: dict[Path, bytes | None] = {}  # MD5 серверных FLAC, прочитанные заранее (prefetch_md5)


def prefetch_md5(paths: list[Path], workers: int = 16):
    """MD5 серверных FLAC — параллельно: по SMB каждое открытие ~12 мс, подряд 10 тыс.
    файлов — 2 минуты, в 16 потоков — секунды."""
    todo = [p for p in paths if p not in _md5_cache]
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            _md5_cache.update(zip(todo, ex.map(flac_md5, todo)))


def flac_md5(p: Path) -> bytes | None:
    """MD5 звука из STREAMINFO — первые 42 байта файла (остальные блоки не читаются)."""
    if p in _md5_cache:
        return _md5_cache[p]
    try:
        with _quiet_close(p) as f:
            head = f.read(42)
    except OSError:
        return None
    if len(head) < 42 or head[:4] != b"fLaC" or head[4] & 0x7F != 0:  # STREAMINFO — первый блок
        return None
    md5 = head[26:42]
    return md5 if any(md5) else None  # нули — MD5 не записан


def audio_differs(src: Path, dst: Path, s: os.stat_result, d: os.stat_result) -> bool:
    """Тот же файл аудио на сервере — другой звук? У FLAC — по MD5 звука (дата не в счёт:
    размер у звука той же длины совпадает); без MD5 — по размеру."""
    if src.suffix.lower() == ".flac":
        a, b = flac_md5(src), flac_md5(dst)
        if a and b:
            return a != b
    # без MD5: другой размер или локальный новее (перерендерен под тем же именем и той же длины);
    # на сервере новее — дату сбила шара, файл тот же
    return s.st_size != d.st_size or s.st_mtime > d.st_mtime + MTIME_WINDOW


def flac_vs_wav_differs(src: Path, wav_st: os.stat_result) -> bool:
    """Локально FLAC, на сервере тот же файл несжатым WAV: сравнение без чтения WAV с сервера.
    В нашем FLAC есть размер исходного WAV (заголовок или .ptchunks) и его дата (flac её переносит).
    Другой размер или локальный новее — изменён; на сервере новее при том же размере — дату сбили."""
    size = wav_size_from_flac(src)
    if size is None:  # FLAC сделан не бэкапом — сравнить не с чем, как раньше по имени
        return False
    return size != wav_st.st_size or src.stat().st_mtime > wav_st.st_mtime + MTIME_WINDOW


def ref_differs(src: Path, stub: Path, fix: bool = True) -> bool:
    """На сервере вместо файла ссылка-дубликат .ptref: локальный файл уже другой? Та же дата — тот же
    (ссылка получает дату исходника); иначе сравнение по содержимому: «тот же файл» — SHA-256,
    «тот же звук» — MD5 звука. Совпало — у ссылки правится дата (её сбила шара)."""
    try:
        s, d = src.stat(), stub.stat()
        if abs(s.st_mtime - d.st_mtime) <= MTIME_WINDOW:
            return False
        h, _, _ = read_stub(stub)
        if h["kind"] == "same" and Path(h.get("name", "")).suffix.lower() == src.suffix.lower():
            differs = s.st_size != h["size"] or file_hash(src) != h["sha256"]
        elif h["kind"] == "same":  # ссылка на тот же FLAC, а локально он уже WAV (распакован)
            a, b = wav_audio_md5(src), flac_md5(stub_target(stub, h))
            differs = bool(a and b and a != b)
        else:
            a = flac_md5(src) if src.suffix.lower() == ".flac" else wav_audio_md5(src)
            differs = bool(a and a.hex() != h["md5"])
        if not differs and fix:  # не в пробном прогоне
            os.utime(stub, (s.st_atime, s.st_mtime))
        return differs
    except (OSError, ValueError, KeyError):
        return False  # не прочитать — как раньше, по имени


def versioned_name(p: Path) -> Path:
    """Session.ptx -> Session_2026-09-29_14-30-05.ptx (дата изменения старой версии)."""
    ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(p.stat().st_mtime))
    cand = p.with_name(f"{p.stem}_{ts}{p.suffix}")
    i = 2
    while cand.exists():
        cand = p.with_name(f"{p.stem}_{ts}_{i}{p.suffix}")
        i += 1
    return cand


def walk_pruned(src: Path, skip: set[Path]):
    """os.walk без каталогов из skip (вложенные сессии при проходе «прочих файлов»)."""
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if d / n not in skip)
        yield d, sorted(filenames)


def sync_mtime(src: Path, dst: Path, remote_st: os.stat_result | None = None) -> bool:
    """Ставит файлу на сервере дату исходника, если размер совпадает, а дата нет.

    Некоторые SMB-подключения сбрасывают дату при переименовании, которое rsync
    делает после записи; без исправления неизменённые файлы копировались бы
    при каждом запуске, а сверка перед --delete-source не проходила бы."""
    try:
        s = src.stat()
        d = remote_st or dst.stat()
        if d.st_size == s.st_size and abs(d.st_mtime - s.st_mtime) > MTIME_WINDOW:
            os.utime(dst, (s.st_atime, s.st_mtime))
            return True
    except OSError:
        pass
    return False


def fix_mtimes(ui: UI, items: list[str], base: Path, dest: Path) -> int:
    fixed = 0
    for key in items:
        src = base / key
        if src.is_file() and not src.is_symlink():
            fixed += sync_mtime(src, dest / key)
    if fixed:
        ui.debug(f"восстановлены даты на сервере: {fixed} файлов")
    return fixed


# ------------------------------------------- дедупликация аудио внутри бэкапа --
# Одинаковое аудио в бэкапе (в разных сессиях, в Source и т.п.) уходит на сервер
# один раз; на месте копии кладётся <имя>.ptref со ссылкой на оригинал:
#   same  — файл совпадает байт в байт (размер + SHA-256): при восстановлении копируется;
#   audio — совпадает только звук (MD5 звука из FLAC + параметры), а заголовок
#           и блоки (umid, regn, bext…) свои: они лежат в самом .ptref, звук берётся
#           из оригинала. Справочник «что уже лежит на сервере» — <проект>/.ptdedup.json.

PTREF_EXT = ".ptref"
PTREF_MAGIC = b"PTREF1\n"
DEDUP_INDEX = ".ptdedup.json"
DEDUP_MIN = 100 << 20  # дубликаты меньше 100 МБ (как хранятся: FLAC + .ptchunks) не трогаем


class _quiet_close:
    """open(..., "rb"), но ошибка при закрытии уже прочитанного файла не считается:
    SMB-шара на отдельных файлах отвечает EBADF на close() (данные при этом целы)."""
    def __init__(self, f):
        self.fh = open(f, "rb")

    def __enter__(self):
        return self.fh

    def __exit__(self, *exc):
        try:
            self.fh.close()
        except OSError:
            pass


def flac_meta(f: Path) -> dict:
    """STREAMINFO (bps, ch, rate, samples, md5) и сохранённые RIFF-блоки WAV."""
    m = {"bps": 0, "ch": 0, "rate": 0, "samples": 0, "md5": b"", "riff": []}
    with _quiet_close(f) as fh:
        if fh.read(4) != b"fLaC":
            raise ValueError("не FLAC")
        while True:
            h = fh.read(4)
            if len(h) < 4:
                raise ValueError("обрыв метаданных")
            kind, n = h[0] & 0x7F, int.from_bytes(h[1:], "big")
            d = fh.read(n)
            if kind == 0:
                m["rate"] = (d[10] << 12) | (d[11] << 4) | (d[12] >> 4)
                m["ch"] = ((d[12] >> 1) & 7) + 1
                m["bps"] = (((d[12] & 1) << 4) | (d[13] >> 4)) + 1
                m["samples"] = ((d[13] & 0x0F) << 32) | int.from_bytes(d[14:18], "big")
                m["md5"] = d[18:34]
            elif kind == 2 and d[:4] == b"riff":
                m["riff"].append(d[4:])
            if h[0] & 0x80:
                return m


def flac_header(f: Path, meta: dict) -> tuple[bytes, bytes, int] | None:
    """Всё, кроме звука, исходного WAV этого FLAC: (начало до звука, конец после,
    размер WAV) — из .ptchunks или из сохранённых --keep-foreign-metadata блоков."""
    side = f.with_suffix(CHUNKS_EXT)
    if side.exists():
        with open(side, "rb") as fh:
            _, off, _, size, _ = CHUNKS_HEAD.unpack(fh.read(CHUNKS_HEAD.size))
            head = fh.read(off)
            return head, fh.read(), size
    riff = meta["riff"]
    if not riff or riff[0][:4] not in (b"RIFF", b"RF64", b"BW64"):
        return None
    total = struct.unpack("<I", riff[0][4:8])[0] + 8
    data_len = None
    ds = next((c for c in riff[1:] if c[:4] == b"ds64"), None)
    if riff[0][:4] != b"RIFF" and ds:
        total, data_len = struct.unpack("<QQ", ds[8:24])
        total += 8
    head, tail, seen = [riff[0][:12]], [], False
    for c in riff[1:]:
        if c[:4] == b"data":
            n = struct.unpack("<I", c[4:8])[0]
            if n == 0xFFFFFFFF and data_len is not None:
                n = data_len
            data_len, seen = n, True
            head.append(c[:8])
            if n & 1:
                tail.append(b"\0")
        else:
            (tail if seen else head).append(c)
    if not seen:
        return None
    h, t = b"".join(head), b"".join(tail)
    return (h, t, total) if len(h) + data_len + len(t) == total else None


def file_keys(f: Path) -> list[tuple[str, str, dict]]:
    """Ключи для поиска дубликатов: («same», …) и для FLAC ещё («audio», …)."""
    size = f.stat().st_size
    is_flac = f.suffix.lower() == ".flac"
    side = f.with_suffix(CHUNKS_EXT)
    has_side = is_flac and side.exists()
    if size + (side.stat().st_size if has_side else 0) < DEDUP_MIN:  # вместе с блоками
        return []
    keys = []
    if not has_side:  # с .ptchunks файл неполный — только «тот же звук»
        sha = file_hash(f)
        keys.append(("same", f"same:{size}:{sha}", {"size": size, "sha256": sha}))
    if is_flac:
        try:
            m = flac_meta(f)
            hdr = flac_header(f, m)
        except (OSError, ValueError, struct.error):
            m, hdr = None, None
        if m and any(m["md5"]) and hdr:
            key = f"audio:{m['md5'].hex()}:{m['samples']}:{m['ch']}:{m['bps']}:{m['rate']}"
            keys.append(("audio", key, {"md5": m["md5"].hex(), "bps": m["bps"], "head": hdr[0],
                                        "tail": hdr[1], "wav_size": hdr[2]}))
    return keys


def make_stub(kind: str, meta: dict, target: str, name: str) -> bytes:
    h = {"v": 1, "kind": kind, "target": target, "name": name}
    payload = b""
    if kind == "same":
        h.update(size=meta["size"], sha256=meta["sha256"])
    else:
        h.update(md5=meta["md5"], bps=meta["bps"], wav_size=meta["wav_size"],
                 head_len=len(meta["head"]), tail_len=len(meta["tail"]),
                 out=str(Path(name).with_suffix(".wav")))
        payload = meta["head"] + meta["tail"]
    return PTREF_MAGIC + json.dumps(h, ensure_ascii=False).encode() + b"\n" + payload


def read_stub(p: Path) -> tuple[dict, bytes, bytes]:
    """(описание, начало WAV, конец WAV) из .ptref."""
    with open(p, "rb") as fh:
        if fh.readline() != PTREF_MAGIC:
            raise ValueError(f"{p.name}: не файл-ссылка")
        h = json.loads(fh.readline())
        rest = fh.read()
    head = rest[:h.get("head_len", 0)]
    return h, head, rest[len(head):]


def stub_target(stub: Path, h: dict) -> Path:
    return Path(os.path.normpath(stub.parent / h["target"]))


class Dedup:
    """Дубликаты внутри одного бэкапа (папки, переданной скрипту). Канонический
    файл — первый встреченный; справочник на сервере обновляется только после
    успешной отправки (commit), при ошибке отправки — откат (rollback)."""

    def __init__(self, ui: UI, root: Path):
        self.ui, self.root = ui, root
        self.index: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self.loaded = False
        self.count = self.saved = 0

    def server_root(self, dest: Path) -> Path:
        return dest / self.root.name

    def load(self, dest: Path):
        if self.loaded:
            return
        self.loaded = True
        try:
            with open(self.server_root(dest) / DEDUP_INDEX, encoding="utf-8") as fh:
                self.index = json.load(fh).get("keys", {})
        except (OSError, ValueError):
            self.index = {}

    def check(self, f: Path, dest: Path):
        """Канонический файл для f или None (тогда f сам становится каноническим)."""
        self.load(dest)
        keys = file_keys(f)
        for kind, key, meta in keys:
            rel = self.pending.get(key)
            if rel is None and key in self.index:
                rel = self.index[key]
                if not (self.server_root(dest) / rel).exists():  # оригинал пропал
                    del self.index[key]
                    rel = None
            if rel is not None and rel != f.relative_to(self.root).as_posix():
                return kind, rel, meta
        for _, key, _ in keys:
            self.pending[key] = f.relative_to(self.root).as_posix()
        return None

    def write_stubs(self, stubs: list, dest: Path):
        for src, kind, canon, meta in stubs:
            rel = src.relative_to(self.root)
            stub = self.server_root(dest) / rel.with_suffix(PTREF_EXT)
            target = os.path.relpath(canon, start=rel.parent.as_posix() or ".")
            tmp = stub.with_name(stub.name + ".part")
            tmp.write_bytes(make_stub(kind, meta, target, src.name))
            tmp.rename(stub)
            st = src.stat()
            os.utime(stub, (st.st_atime, st.st_mtime))

    def commit(self, dest: Path):
        if not self.pending:
            return
        self.index.update(self.pending)
        self.pending = {}
        path = self.server_root(dest) / DEDUP_INDEX
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps({"v": 1, "keys": self.index}, ensure_ascii=False), encoding="utf-8")
        tmp.rename(path)

    def rollback(self):
        self.pending = {}

    def move_original(self, ui: UI, dest: Path, old_rel: str, new_rel: str):
        """Оригинал дубликатов на сервере переименован (его перезаписали): ссылки .ptref на него
        и справочник переводятся на новое имя — дубликаты в других сессиях сохраняют свой звук."""
        sroot = self.server_root(dest)
        old_abs = os.path.normpath(sroot / old_rel)
        n = 0
        for stub in sroot.rglob("*" + PTREF_EXT):
            try:
                with open(stub, "rb") as fh:
                    magic, line, rest = fh.readline(), fh.readline(), fh.read()
                h = json.loads(line)
                if os.path.normpath(stub.parent / h["target"]) != old_abs:
                    continue
                h["target"] = os.path.relpath(sroot / new_rel, start=stub.parent)
                st = stub.stat()
                tmp = stub.with_name(stub.name + ".part")
                tmp.write_bytes(magic + json.dumps(h, ensure_ascii=False).encode() + b"\n" + rest)
                tmp.rename(stub)
                os.utime(stub, (st.st_atime, st.st_mtime))  # дата ссылки = дата её исходника
                n += 1
            except (OSError, ValueError, KeyError) as e:
                ui.warn(f"{stub.name}: ссылку на {Path(old_rel).name} не перевести: {e}")
        for k, v in list(self.index.items()):
            if v == old_rel:
                self.index[k] = new_rel
        path = sroot / DEDUP_INDEX
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps({"v": 1, "keys": self.index}, ensure_ascii=False), encoding="utf-8")
        tmp.rename(path)
        ui.debug(f"оригинал дубликатов {old_rel} -> {new_rel}: ссылок переведено {n}")


# ------------------------------- рабочие копии видео (mix_prep) вместо mov ---
# <title>_Workcopy_<дата>_<fps>.mov — ProRes-копия от mix_prep из исходного видео
# (mp4 и т.п.). С --no-workcopy mov на сервер не идёт: в папке исходных видео
# ведётся один файл Workcopies.ptwork с описанием всех их рабочих копий
# (в т.ч. в подпапках <title>_Work); pt_restore --workcopy пересобирает их.

WORKCOPY_RE = re.compile(r"^(?P<title>.+)_Workcopy_\d{8}_\d+\.mov$", re.I)
PTWORK_FILE = "Workcopies.ptwork"
MIX_PREP = "/usr/local/bin/mix_prep"
VIDEO_SRC_EXTS = {".mp4", ".mkv", ".m4v", ".avi", ".m2ts"}
VIDEO_EXTS = VIDEO_SRC_EXTS | {".mov", ".mxf", ".webm", ".wmv", ".mpg", ".mpeg", ".dv", ".ts"}  # для --no-video
_mix_prep_mod = None
_probe_cache: dict[str, dict | None] = {}
_ptwork_cache: dict[str, dict] = {}


def mix_prep_title(name: str) -> str | None:
    """Имя рабочей копии, которое mix_prep даст файлу name (его же функцией)."""
    global _mix_prep_mod
    try:
        if _mix_prep_mod is None:
            from importlib.machinery import SourceFileLoader
            _mix_prep_mod = SourceFileLoader("mix_prep", MIX_PREP).load_module()
        return _mix_prep_mod.assign_title(name)
    except Exception:  # noqa: BLE001 — нет mix_prep/guessit или имя не разобрать
        return None


def video_probe(p: Path) -> dict | None:
    """Кадры, частота, размер, звуковые дорожки и параметры mix_prep из метаданных."""
    key = str(p)
    if key not in _probe_cache:
        r = run_quiet(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
                       "-show_format", str(p)], 60)
        info = None
        if r and r.returncode == 0:
            try:
                j = json.loads(r.stdout)
                v = next(x for x in j["streams"] if x.get("codec_type") == "video")
                num, _, den = v.get("r_frame_rate", "0/1").partition("/")
                fps = int(num) / int(den or 1)
                frames = int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else \
                    round(float(v.get("duration") or j["format"].get("duration") or 0) * fps)
                audio = [(a["index"], a.get("codec_name", ""), a.get("channels", 0))
                         for a in j["streams"] if a.get("codec_type") == "audio"]
                comment = j.get("format", {}).get("tags", {}).get("comment", "")
                params = None
                if comment.startswith("mix_prep "):
                    try:
                        params = json.loads(comment[len("mix_prep "):])
                    except ValueError:
                        params = None
                info = {"frames": frames, "fps": v.get("r_frame_rate"), "w": v.get("width"),
                        "h": v.get("height"), "audio": audio, "mix_prep": params}
            except (ValueError, KeyError, StopIteration, ZeroDivisionError):
                info = None
        _probe_cache[key] = info
    return _probe_cache[key]


def read_ptwork(path: Path) -> dict:
    """Записи Workcopies.ptwork: {путь mov относительно папки: данные}."""
    key = str(path)
    if key not in _ptwork_cache:
        try:
            _ptwork_cache[key] = json.loads(path.read_text(encoding="utf-8")).get("workcopies", {})
        except (OSError, ValueError):
            _ptwork_cache[key] = {}
    return _ptwork_cache[key]


def ptwork_entry_for(mov_server: Path) -> tuple[Path, dict] | None:
    """Запись о рабочей копии (путь на сервере) в Workcopies.ptwork её папки или уровнем выше."""
    for d, rel in ((mov_server.parent, mov_server.name),
                   (mov_server.parent.parent, f"{mov_server.parent.name}/{mov_server.name}")):
        e = read_ptwork(d / PTWORK_FILE).get(rel)
        if e:
            return d / PTWORK_FILE, e
    return None


def workcopy_source(mov: Path, server_dir: Path | None) -> tuple[int, dict] | None:
    """Исходное видео для рабочей копии mov: то же имя по правилу mix_prep и то же
    число кадров и частота. Ищется рядом (уровень 0) и уровнем выше (1), локально
    и на сервере. Возвращает (уровень папки исходника, запись для Workcopies.ptwork)."""
    m = WORKCOPY_RE.match(mov.name)
    if not m:
        return None
    mi = video_probe(mov)
    if not mi or not mi["frames"]:
        return None
    for level in (0, 1):
        dirs = [mov.parent if level == 0 else mov.parent.parent]
        if server_dir:
            dirs.append(server_dir if level == 0 else server_dir.parent)
        for d in dirs:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                if Path(n).suffix.lower() not in VIDEO_SRC_EXTS or n.startswith("._"):
                    continue
                if mix_prep_title(n) != m["title"]:
                    continue
                ci = video_probe(d / n)
                if not ci or ci["frames"] != mi["frames"] or ci["fps"] != mi["fps"]:
                    continue
                mp = mi["mix_prep"] or {}
                mov_audio = mi["audio"][0] if mi["audio"] else None
                if "audio_index" in mp:
                    audio_index = mp["audio_index"]
                else:  # копия сделана старым mix_prep — дорожку подбираем по каналам
                    pick = next((a for a in ci["audio"] if mov_audio and a[2] == mov_audio[2]),
                                ci["audio"][0] if ci["audio"] else None)
                    audio_index = pick[0] if pick and mov_audio else None
                subs = None
                if mp.get("subtitles"):  # в метаданных — относительно mov, храним от папки исходника
                    subs = os.path.relpath(os.path.normpath(mov.parent / mp["subtitles"]),
                                           mov.parent if level == 0 else mov.parent.parent)
                entry = {"source": n, "size": mov.stat().st_size, "frames": mi["frames"],
                         "fps": mi["fps"], "width": mi["w"], "height": mi["h"],
                         "audio_index": audio_index,
                         "audio_24bit": (mp.get("bitdepth") == 24) if mp else
                                        bool(mov_audio and "24" in mov_audio[1]),
                         "timecode": bool(mp.get("timecode")), "subtitles": subs,
                         "params_known": bool(mp)}
                return level, entry
    return None


def plan_workcopy(ui: UI, src: Path, rel: Path, dest_root: Path, stats: dict) -> bool:
    found = workcopy_source(src, dest_root / rel.parent)
    if not found:
        ui.debug(f"{rel}: исходное видео не найдено — mov уйдёт как есть")
        return False
    level, entry = found
    src_dir_rel = rel.parent if level == 0 else rel.parent.parent
    mov_key = src.name if level == 0 else f"{src.parent.name}/{src.name}"
    stats["workcopy"] += 1
    stats["workcopy_bytes"] += entry["size"]
    stats["video_stubs"].append((src, dest_root / src_dir_rel / PTWORK_FILE, mov_key, entry))
    ui.debug(f"{rel}: рабочая копия из {entry['source']} — в {PTWORK_FILE} вместо mov"
             + ("" if entry["params_known"] else " (параметры mix_prep неизвестны: старая копия)"))
    return True


EXTERNAL_VIDEO_DIR = "Video (вне проекта)"  # на сервере внутри проекта: видео сессий, лежащее вне папки проекта
CONVERTER_DIRS = [Path.home() / "ReaperToProTools" / "ptx_tools",
                  Path("/Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools")]


def session_media(ptx: Path, ctype: int = 0x1006) -> list[tuple[Path, bool]] | None:
    """Файлы сессии из каталога ctype: 0x1006 — видео, 0x1004 — аудио (разбор из конвертера Reaper ↔ Pro Tools),
    с поиском файла как у Pro Tools: [(путь, найден)]. None — конвертера на этом Mac нет."""
    conv = next((d for d in CONVERTER_DIRS if (d / "ptx_to_reaper.py").exists()), None)
    if conv is None:
        return None
    if str(conv) not in sys.path:
        sys.path.append(str(conv))
    try:
        from ptx_model import PTX
        import ptx_to_reaper as R
        cat = R.Catalog(PTX(ptx.read_bytes()), ctype)
        out = []
        for i in range(len(cat.files)):
            x, ok = R.resolve_file(cat, i, str(ptx.parent), [], {})
            path = Path(x)
            if not ok and len(path.parts) > 2:
                # Pro Tools пишет путь от имени диска: /Current projects/Video/… = /Volumes/Current projects/Video/…
                alt = Path("/Volumes", *path.parts[1:])
                if alt.is_file():
                    path, ok = alt, True
            out.append((path, ok))
        return out
    except Exception:  # noqa: BLE001 — сессия другого формата: без видео, бэкап не останавливаем
        return []


PTX_VIDEO_CACHE = Path.home() / "Library" / "Application Support" / "pt_backup" / "ptx_videos.json"


def external_videos(ui: UI, root: Path) -> list[Path]:
    """Видео сессий проекта, лежащее вне его папки; о ненайденном — предупреждение.
    Разобранные .ptx кэшируются (размер + дата файла): нетронутая сессия не разбирается заново."""
    found: set[Path] = set()
    missing: dict[str, Path] = {}
    try:
        cache = json.loads(PTX_VIDEO_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    for sess in find_sessions([root]):
        for ptx in sorted(sess.glob("*.ptx")):
            if ptx.name.startswith("._"):
                continue
            st = ptx.stat()
            hit = cache.get(str(ptx))
            if hit and hit[:2] == [st.st_size, st.st_mtime_ns]:
                vids = [(Path(v), Path(v).is_file()) for v in hit[2]]  # есть ли файл — сейчас
            else:
                vids = session_media(ptx)
                if vids is None:
                    ui.debug("конвертер Reaper ↔ Pro Tools не найден — видео вне проекта не ищется")
                    return []
                cache[str(ptx)] = [st.st_size, st.st_mtime_ns, [str(v) for v, _ in vids]]
            for path, ok in vids:
                if ok and not path.resolve().is_relative_to(root):
                    found.add(path)
                elif not ok:
                    missing.setdefault(path.name, path)
    for name, path in sorted(missing.items()):
        ui.warn(f"{root.name}: видео сессии не найдено — {path}")
    try:  # записи удалённых .ptx не копим
        PTX_VIDEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PTX_VIDEO_CACHE.write_text(json.dumps({k: v for k, v in cache.items() if Path(k).exists()},
                                              ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return sorted(found)


def upload_external_videos(ui: UI, root: Path, videos: list[Path], server: Path,
                           no_workcopy: bool) -> tuple[int, int, int]:
    """Видео вне проекта — на сервер в <проект>/Video (вне проекта)/. Рабочая копия mov с найденным
    исходником при no_workcopy: исходник + запись в Workcopies.ptwork, как внутри проекта.
    Возвращает (ошибок, передано файлов, байт)."""
    base = server / root.name / EXTERNAL_VIDEO_DIR

    def dir_for(f: Path) -> Path:
        # /Volumes/Current projects/Video/x.mov -> Video (вне проекта)/Current projects/Video/:
        # одноимённые файлы из разных папок не перезаписывают друг друга
        parts = f.parent.parts[1:]
        return base.joinpath(*(parts[1:] if parts[:1] == ("Volumes",) else parts))

    rsync, progress2 = find_rsync()
    flags = [*RSYNC_FLAGS, f"--modify-window={MTIME_WINDOW}", f"--partial-dir={PARTIAL_DIR}"]
    if progress2:
        flags.append("--info=progress2")
    errors = sent = sent_bytes = 0
    for v in videos:
        send, stub = v, None
        if no_workcopy and WORKCOPY_RE.match(v.name):
            ws = workcopy_source(v, None)
            if ws:
                level, entry = ws
                send = (v.parent if level == 0 else v.parent.parent) / entry["source"]
                stub = (v, None, v.name, entry)  # путь .ptwork — рядом с исходником, ниже
        sdir = dir_for(send)
        if stub:
            stub = (stub[0], sdir / PTWORK_FILE, stub[2], stub[3])
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            flat = base / send.name  # раньше всё внешнее видео лежало прямо в Video (вне проекта)/
            if flat != sdir / send.name and flat.is_file() and not (sdir / send.name).exists() \
                    and flat.stat().st_size == send.stat().st_size:
                flat.rename(sdir / send.name)  # переносим на сервере, а не перекачиваем
            # тот же размер, а на сервере новее: дату сбросила SMB-шара — правим, не перекачивая
            # (локальный новее — перерендерен: пусть rsync сравнит и отправит)
            r_old = sdir / send.name
            if r_old.exists() and r_old.stat().st_mtime > send.stat().st_mtime + MTIME_WINDOW:
                sync_mtime(send, r_old)
            bar = ui.bar(f"  Видео вне проекта · {send.name}", send.stat().st_size, "bytes" if progress2 else "pct")
            rc, err, n = run_rsync(ui, bar, [rsync, *flags, str(send), str(sdir) + "/"])
            if rc != 0:
                bar.close(f"ошибка rsync ({rc})")
                ui.error(f"{send}: {err.strip()[-300:]}")
                errors += 1
                continue
            sync_mtime(send, sdir / send.name)  # шара сбрасывает дату после записи — иначе перекачка каждую ночь
            sent += n
            sent_bytes += int(bar.value) if n else 0
            bar.close(f"отправлено ({v.parent})" if n else f"уже на сервере ({v.parent})")
            if stub:
                write_workcopy_stubs([stub])
        except OSError as e:
            ui.error(f"{send}: {e}")
            errors += 1
    return errors, sent, sent_bytes


def write_workcopy_stubs(stubs: list):
    """Дописывает записи в Workcopies.ptwork (по одному файлу на папку исходников)."""
    by_file: dict[Path, dict] = {}
    for _, path, key, entry in stubs:
        by_file.setdefault(path, {})[key] = entry
    for path, new in by_file.items():
        _ptwork_cache.pop(str(path), None)
        entries = dict(read_ptwork(path))
        entries.update(new)
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps({"v": 1, "workcopies": entries}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.rename(path)
        _ptwork_cache[str(path)] = entries


def build_plan(ui: UI, src_root: Path, base: Path, dest_root: Path, skip: set[Path],
               dry: bool, bar: Bar, dedup: Dedup | None = None,
               dest: Path | None = None, no_workcopy: bool = False,
               no_video: bool = False) -> tuple[list[str], dict, list]:
    """Решает, что передавать, и переименовывает на сервере изменённые файлы сессий.

    - аудио: только отсутствующие на сервере
    - .ptx/.ptf/.pts: побайтовое сравнение; при отличии старая версия на сервере
      переименовывается с датой, новая копируется
    - остальное: отдаётся rsync (сравнение по размеру и дате)
    """
    remote = scan_remote(dest_root, {name_key(p.relative_to(src_root)) for p in skip})
    items: list[str] = []
    stubs: list = []  # (файл, вид, канонический путь, данные) — вместо отправки
    refs: set[Path] = set()
    changed: set[Path] = set()  # перезаписанное аудио (без расширения) — его .ptchunks тоже уходят
    prefetch_md5([dest_root / (d / f).relative_to(src_root) for d, files in walk_pruned(src_root, skip)
                  for f in files if f.lower().endswith(".flac")
                  and name_key((d / f).relative_to(src_root)) in remote])
    dedup_refs = set(dedup.index.values()) if dedup and dest and (dedup.load(dest) or True) else set()
    stats = {"audio_new": 0, "audio_skip": 0, "session_changed": 0, "session_same": 0,
             "dup": 0, "dup_bytes": 0, "workcopy": 0, "workcopy_bytes": 0, "video_stubs": []}
    for d, filenames in walk_pruned(src_root, skip):
        items.append(str(d.relative_to(base)))  # каталоги (в т.ч. пустые)
        for f in filenames:
            if f == RESTORED_FILE:  # служебная запись pt_restore — только локально
                continue
            if no_video and Path(f).suffix.lower() in VIDEO_EXTS:  # --no-video: видео на сервер не идёт
                stats["video_skip"] = stats.get("video_skip", 0) + 1
                bar.advance()
                continue
            src = d / f
            rel = src.relative_to(src_root)
            key = str(src.relative_to(base))
            if rel.suffix == PTREF_EXT and name_key(rel) in remote:
                # локальная ссылка — копия серверной (распаковка без --source-dups): главная — на сервере,
                # её могли перевести на прежнюю версию оригинала
                stats["other_same"] = stats.get("other_same", 0) + 1
                bar.advance()
                continue
            r_same = remote.get(name_key(rel)) if is_audio(rel) else None
            st_src = src.lstat() if r_same is not None else None
            if r_same is not None and audio_differs(src, dest_root / rel, st_src, r_same):
                # то же имя, другой звук и дата — файл перезаписали (например, после распаковки):
                # прежняя версия на сервере остаётся с датой в имени, новая уходит
                stats["audio_changed"] = stats.get("audio_changed", 0) + 1
                changed.add(src.with_suffix(""))
                dst = dest_root / rel
                new = versioned_name(dst)
                ui.debug(f"на сервере: {rel} -> {new.name} (аудио изменено)")
                side_k = name_key(rel.with_suffix(CHUNKS_EXT))
                if not dry:
                    old_rel = src.relative_to(dedup.root).as_posix() if dedup else None
                    if old_rel in dedup_refs:
                        # оригинал для дубликатов: сначала ссылки — на имя, под которым останется прежняя
                        # версия (оно повторяемо: дата файла), потом переименование. При обрыве между
                        # шагами ссылки не укажут на файл, куда ляжет новый звук
                        dedup.move_original(ui, dest, old_rel,
                                            Path(old_rel).with_name(new.name).as_posix())
                    dst.rename(new)
                    if side_k in remote:  # блоки прежней версии — под тем же новым именем, иначе пара распадётся
                        dst.with_suffix(CHUNKS_EXT).rename(new.with_suffix(CHUNKS_EXT))
                remote.pop(side_k, None)
                items.append(key)
            elif (rel.suffix.lower() == ".flac" and name_key(rel) not in remote
                  and (wk := next((k for k in (name_key(rel.with_suffix(".wav")), name_key(rel.with_suffix(".WAV")))
                                   if k in remote), None))
                  and flac_vs_wav_differs(src, remote[wk])):
                # на сервере был несжатый WAV, а файл перезаписали: WAV остаётся с датой в имени, уходит FLAC
                stats["audio_changed"] = stats.get("audio_changed", 0) + 1
                changed.add(src.with_suffix(""))
                old_wav = dest_root / rel.with_name(Path(wk).name)
                ui.debug(f"на сервере: {old_wav.name} -> {versioned_name(old_wav).name} (файл изменён)")
                if not dry:
                    old_wav.rename(versioned_name(old_wav))
                items.append(key)
            elif (is_audio(rel) and name_key(rel) not in remote
                  and name_key(rel.with_suffix(PTREF_EXT)) in remote
                  and ref_differs(src, dest_root / rel.with_suffix(PTREF_EXT), not dry)):
                # на сервере была ссылка-дубликат, а файл перезаписали: ссылка остаётся с датой в имени,
                # уходит сам файл (и его .ptchunks)
                stats["audio_changed"] = stats.get("audio_changed", 0) + 1
                changed.add(src.with_suffix(""))
                stub = dest_root / rel.with_suffix(PTREF_EXT)
                ui.debug(f"на сервере: {stub.name} -> {versioned_name(stub).name} (файл изменён)")
                if not dry:
                    stub.rename(versioned_name(stub))
                items.append(key)
            elif is_audio(rel):
                if audio_present(rel, remote):
                    stats["audio_skip"] += 1
                    if not dry and name_key(rel) in remote:  # дата, сброшенная сервером
                        sync_mtime(src, dest_root / rel, remote[name_key(rel)])
                else:
                    m = dedup.check(src, dest) if dedup and dest else None
                    if m:
                        stubs.append((src, *m))
                        refs.add(src.with_suffix(""))
                        stats["dup"] += 1
                        side = src.with_suffix(CHUNKS_EXT)
                        stats["dup_bytes"] += src.stat().st_size + (
                            side.stat().st_size if side.exists() else 0)
                        ui.debug(f"дубликат ({m[0]}): {rel} -> {m[1]}")
                    else:
                        stats["audio_new"] += 1
                        items.append(key)
            elif rel.suffix.lower() in SESSION_EXTS and name_key(rel) in remote:
                dst = dest_root / rel
                if src.stat().st_size == remote[name_key(rel)].st_size and same_content(src, dst):
                    stats["session_same"] += 1
                    if not dry:
                        sync_mtime(src, dst, remote[name_key(rel)])
                else:
                    stats["session_changed"] += 1
                    new = versioned_name(dst)
                    ui.debug(f"на сервере: {rel} -> {new.name}")
                    if not dry:
                        dst.rename(new)
                    items.append(key)
            elif rel.suffix == CHUNKS_EXT and src.with_suffix("") in changed:
                dst = dest_root / rel  # блоки к изменённому аудио: прежние — с датой, новые уходят
                if name_key(rel) in remote and not dry:
                    dst.rename(versioned_name(dst))
                items.append(key)
            elif rel.suffix == CHUNKS_EXT and (audio_present(rel.with_suffix(".wav"), remote)
                                               or src.with_suffix("") in refs):
                stats["audio_skip"] += 1  # на сервере исходный WAV или ссылка — блоки не нужны
            elif (no_workcopy and WORKCOPY_RE.match(f) and name_key(rel) not in remote
                  and (ptwork_entry_for(dest_root / rel)
                       or plan_workcopy(ui, src, rel, dest_root, stats))):
                pass  # рабочая копия видео: вместо mov — .ptwork (или он уже на сервере)
            else:
                # то же правило, что у rsync (размер + дата): заранее знаем объём передачи
                r = remote.get(name_key(rel))
                st = src.lstat()
                if r and r.st_size == st.st_size and r.st_mtime > st.st_mtime + MTIME_WINDOW:
                    # на сервере новее при том же размере: дату сбили шара или копирование в Finder,
                    # а локально файл с тех пор не менялся — правим дату, не перекачивая
                    if not dry:
                        sync_mtime(src, dest_root / rel, r)
                    stats["other_same"] = stats.get("other_same", 0) + 1
                elif r and r.st_size == st.st_size and abs(r.st_mtime - st.st_mtime) <= MTIME_WINDOW:
                    stats["other_same"] = stats.get("other_same", 0) + 1
                else:
                    items.append(key)
            bar.advance()
    return items, stats, stubs


def step_rsync(ui: UI, src: Path, base: Path, skip: set[Path], smb: SMB | None,
               local_dest: Path | None, retries: int, delay: float, dry: bool,
               title: str, dedup: Dedup | None = None, no_workcopy: bool = False,
               prune: bool = False, no_video: bool = False) -> int:
    """Передача src (сессии или папки проекта без сессий из skip) в dest/<src от base>.
    prune — после отправки оставить у сессии на сервере один архив бэкапов."""
    rsync, progress2 = find_rsync()
    flags = [*RSYNC_FLAGS, f"--modify-window={MTIME_WINDOW}", f"--partial-dir={PARTIAL_DIR}"]
    if progress2:
        flags += ["--info=progress2", "--no-inc-recursive"]
    else:
        ui.debug(f"{rsync} без --info=progress2 — прогресс по файлам (brew install rsync)")
    if dry:
        flags.append("-n")

    state: dict = {}

    def attempt(n: int, retry: str):
        try:
            dest = smb.ensure() if smb else local_dest
            assert dest is not None
            if not smb:
                dest.mkdir(parents=True, exist_ok=True)
            total = sum(len(f) for _, f in walk_pruned(src, skip))
            bar = ui.bar(f"{title}: сравнение{retry}", total)
            if dedup:
                dedup.rollback()
            items, st, stubs = build_plan(ui, src, base, dest / src.relative_to(base), skip,
                                          dry, bar, dedup, dest, no_workcopy, no_video)
            dup = (f", дубликатов: {st['dup']} ({fmt_bytes(st['dup_bytes'])})"
                   if st["dup"] else "")
            if st["workcopy"]:
                dup += f", рабочих копий видео не отправлено: {st['workcopy']} ({fmt_bytes(st['workcopy_bytes'])})"
            if st.get("video_skip"):
                dup += f", видео пропущено: {st['video_skip']}"
            changed_audio = f", изменённых аудио: {st['audio_changed']}" if st.get("audio_changed") else ""
            bar.close(f"новых аудио: {st['audio_new']}{changed_audio}, "
                      f"изменённых сессий: {st['session_changed']}{dup}")
        except OSError as e:
            if ui.active:
                ui.active.close("ошибка")
            ui.error(f"сравнение с сервером: {e}")
            return None
        with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as lf:
            lf.write("\n".join(items) + "\n")
        total = sum((base / k).lstat().st_size for k in items if not (base / k).is_dir())
        bar = (ui.bar(f"{title}{retry}", total, "bytes") if progress2
               else ui.bar(f"{title}{retry}", 100, "pct"))
        cmd = [rsync, *flags, f"--files-from={lf.name}", str(base) + "/", str(dest) + "/"]
        try:
            rc, err, transferred = run_rsync(ui, bar, cmd)
        finally:
            os.unlink(lf.name)
        state.update(dest=dest, items=items, st=st, stubs=stubs, bar=bar, transferred=transferred)
        return rc, err, bar

    if not rsync_retry(ui, retries, delay, attempt, lambda: not smb or smb.healthy()):
        if dedup:
            dedup.rollback()
        return 1
    dest, items, st, stubs, bar = (state[k] for k in ("dest", "items", "st", "stubs", "bar"))
    if not dry:
        ui.stats["files"] += state.get("transferred") or 0
        ui.stats["bytes"] += bar.value if bar.unit == "bytes" else 0
    bar.set(bar.total)
    bar.close(f"готово, передано файлов: {state['transferred']}" if progress2 else "готово")
    if dry:
        if dedup:
            dedup.rollback()
        return 0
    fix_mtimes(ui, items, base, dest)
    if prune:  # архивы этой сессии слиты в новый .ptbak в этом запуске
        prune_server_archives(ui, src, dest / src.relative_to(base))
    try:
        write_workcopy_stubs(st["video_stubs"])
    except OSError as e:
        ui.error(f"описания рабочих копий видео: {e}")
        return 1
    if dedup:
        try:
            dedup.write_stubs(stubs, dest)
            dedup.commit(dest)
        except OSError as e:
            dedup.rollback()
            ui.error(f"ссылки на дубликаты: {e}")
            return 1
    return 0


def rsync_retry(ui: UI, retries: int, delay: float, attempt, alive) -> bool:
    """Общий цикл повторов rsync (бэкап и восстановление). attempt(n, метка) -> (rc, err, bar)
    или None, если попытка не состоялась (ошибка уже выведена); alive() — шара в порядке.
    После зависания следующая попытка сама переподключает шару. True — передача удалась."""
    for n in range(1, retries + 1):
        res = attempt(n, "" if n == 1 else f" (повтор {n}/{retries})")
        if res is not None:
            rc, err, bar = res
            if rc == 0:
                return True
            if rc == RSYNC_STALLED:
                bar.close("передача зависла")
                ui.warn(f"нет передачи данных {stall_text()} — проверка шары и повтор")
                continue
            bar.close(f"ошибка rsync ({rc})")
            if err.strip():
                ui.error("\n".join(err.strip().splitlines()[-10:]))
            if rc == 20:  # получен сигнал
                raise KeyboardInterrupt
            # 23/24 — часть файлов не передана/исчезла; если шара жива, повтор не поможет
            if rc in (23, 24) and alive():
                ui.warn("rsync завершился с частичной передачей (см. ошибки выше)")
                return False
        if n < retries:
            ui.warn("проверка подключения и повтор…")
            time.sleep(min(delay * n, 60))
    return False


def has_loose_files(root: Path, sessions: list[Path]) -> bool:
    """Есть ли в проекте файлы вне сессий (видео, документы и т.п.)."""
    if root in sessions:  # передана сама сессия
        return False
    return any(f not in JUNK for _, files in walk_pruned(root, set(sessions)) for f in files)


# ------------------------------------------------ --daily: временная копия --

def clone_file(src: Path, dst: Path):
    """APFS-клон (мгновенно, без места на диске); иначе обычное копирование."""
    st = src.lstat()
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
        return
    if _clonefile is None or _clonefile(bytes(src), bytes(dst), 0) != 0:
        shutil.copyfile(src, dst)
    shutil.copystat(src, dst)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def _load_clonefile():
    try:
        import ctypes
        f = ctypes.CDLL(None, use_errno=True).clonefile
        f.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        return f
    except (OSError, AttributeError):
        return None


_clonefile = _load_clonefile()


def staging_base(root: Path, temp_dir: Path | None) -> Path:
    return temp_dir or root.parent / STAGING_DIR


def audio_id(rel: Path) -> str:
    """Имя аудио для сравнения списков: WAV и FLAC с одним именем — один файл."""
    if rel.suffix.lower() in (".wav", ".flac", PTREF_EXT):
        rel = rel.with_suffix(".flac")
    return name_key(rel)


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def audio_changed(src: Path, rel: Path, remote: dict, droot: Path, fix: bool = True) -> bool:
    """Аудио есть на сервере (в каком-то виде), но его перезаписали — те же проверки, что при
    отправке: тот же вид — дата, затем MD5 (без MD5 — размер/дата); WAV ↔ FLAC — wav_changed;
    FLAC ↔ несжатый WAV — по размеру/дате из FLAC; ссылка .ptref — ref_differs."""
    k = name_key(rel)
    try:
        st = src.lstat()
        if k in remote:
            r = remote[k]
            if r.st_size == st.st_size and abs(r.st_mtime - st.st_mtime) <= MTIME_WINDOW:
                return False
            return audio_differs(src, droot / rel, st, r)
        if src.suffix.lower() == ".wav" and name_key(rel.with_suffix(".flac")) in remote:
            return wav_changed(src, rel, remote, droot)
        if src.suffix.lower() == ".flac":
            wk = next((x for x in (name_key(rel.with_suffix(".wav")), name_key(rel.with_suffix(".WAV")))
                       if x in remote), None)
            if wk:
                return flac_vs_wav_differs(src, remote[wk])
        if name_key(rel.with_suffix(PTREF_EXT)) in remote:
            return ref_differs(src, droot / rel.with_suffix(PTREF_EXT), fix)
    except OSError:
        pass
    return False


def session_unchanged(session: Path, root: Path, dest_root: Path, remote: dict,
                      no_video: bool = False, no_workcopy: bool = True, fix: bool = True) -> bool:
    """Сессия уже на сервере в актуальном виде: тот же список аудио и ни одно не перезаписано,
    те же .ptx/.ptf/.pts (по SHA-256), прочие файлы — не новее и того же размера."""
    prefix = name_key(session.relative_to(root)) + "/"
    local_audio = {audio_id(p.relative_to(root)) for p in session.rglob("*")
                   if p.is_file() and is_audio(p) and not p.name.startswith("._")}
    server_audio = {audio_id(Path(k)) for k in remote
                    if k.startswith(prefix) and (is_audio(Path(k)) or k.endswith(PTREF_EXT))}
    if local_audio != server_audio:
        return False
    for p in session.rglob("*"):
        if not p.is_file() or p.name.startswith("._") or p.name in JUNK or p.name == WAVECACHE:
            continue
        rel = p.relative_to(root)
        if is_audio(p):  # перезаписанный под тем же именем (во всех видах на сервере)
            if audio_changed(p, rel, remote, dest_root, fix):
                return False
            continue
        if BACKUPS_DIR in rel.parts or p.suffix == CHUNKS_EXT:
            continue  # бэкапы — ниже, по архиву; блоки — вместе со своим аудио
        if p.suffix.lower() in SESSION_EXTS and p.parent == session:
            if name_key(rel) not in remote or file_hash(p) != file_hash(dest_root / rel):
                return False
            continue
        if no_video and p.suffix.lower() in VIDEO_EXTS:
            continue
        r = remote.get(name_key(rel))
        if p.suffix == PTREF_EXT and r is not None:
            continue  # копия серверной ссылки (распаковка без --source-dups): главная — на сервере
        if r is None:
            if no_workcopy and WORKCOPY_RE.match(p.name):
                continue  # рабочая копия: на сервере вместо неё запись в Workcopies.ptwork
            return False
        st = p.lstat()
        if r.st_size != st.st_size or st.st_mtime > r.st_mtime + MTIME_WINDOW:
            return False
    # новые автобэкапы Pro Tools, даже если сама сессия не менялась: у архива на сервере дата
    # самого свежего бэкапа внутри (ставится при упаковке) — локальный новее, значит, есть новые
    bdir = session / BACKUPS_DIR
    newest = max((f.stat().st_mtime for f in bdir.rglob("*") if f.is_file() and f.name not in JUNK),
                 default=None) if bdir.is_dir() else None
    if newest is not None:
        bprefix = prefix + name_key(BACKUPS_DIR)
        arcs = [r.st_mtime for k, r in remote.items() if k.startswith(bprefix) and "/" not in k[len(prefix):]
                and Path(k).suffix.lower() in (".zip", PTBAK_EXT)]
        if not arcs or newest > max(arcs) + MTIME_WINDOW:
            return False
    return True


def wav_audio_md5(w: Path) -> bytes | None:
    """MD5 звука WAV так, как его считает flac (по сэмплам; 8 бит — без знака -> со знаком).
    None — WAV, который flac не сжимает, или не прочитать."""
    info = wave_info(w)
    if info["problem"] or not info["audio_len"]:
        return None
    flip = bytes(x ^ 0x80 for x in range(256)) if info["bits"] <= 8 else None
    md5, left = hashlib.md5(), info["audio_len"]
    try:
        with _quiet_close(w) as fh:
            fh.seek(info["audio_off"])
            while left > 0:
                c = fh.read(min(left, 8 << 20))
                if not c:
                    return None
                md5.update(c.translate(flip) if flip else c)
                left -= len(c)
    except OSError:
        return None
    return md5.digest()


def wav_changed(src: Path, rel: Path, remote: dict, droot: Path) -> bool:
    """WAV с тем же именем, что FLAC на сервере, перезаписан? Дата FLAC = дата исходного WAV:
    совпала — тот же файл; иначе решает MD5 звука (дата могла просто сбиться на SMB)."""
    if src.suffix.lower() != ".wav":
        return False
    r = remote.get(name_key(rel.with_suffix(".flac")))
    if r is None:  # на сервере ссылка-дубликат или сам WAV — как раньше, по имени
        return False
    if abs(r.st_mtime - src.lstat().st_mtime) <= MTIME_WINDOW:
        return False
    a, b = flac_md5(droot / rel.with_suffix(".flac")), wav_audio_md5(src)
    return not (a and b) or a != b  # MD5 не узнать — пусть сожмётся, дальше решит сверка FLAC


DAILY_STATE = Path.home() / "Library" / "Application Support" / "pt_backup" / "daily_state.json"
DAILY_FULL_EVERY = 7 * 86400  # раз в неделю --daily всё равно сверяется с сервером полностью


def fingerprint(d: Path, skip: set[Path] = frozenset()) -> str:
    """Отпечаток папки: пути, размеры и даты файлов, а у .ptx/.ptf/.pts — SHA-256
    содержимого (без WaveCache и служебных). Совпал с прошлым удачным --daily — ничего не менялось."""
    h = hashlib.sha256()
    for dp, files in walk_pruned(d, set(skip)):
        for f in files:
            if f in JUNK or f == WAVECACHE:
                continue
            p = Path(dp, f)
            try:
                st = p.lstat()
                # основной файл сессии — по содержимому: Pro Tools может пересохранить его с тем же
                # размером; бэкапы в Session File Backups он не переписывает (новый — новый номер),
                # их хватает размера и даты
                main = p.suffix.lower() in SESSION_EXTS and not f.startswith("._") \
                    and BACKUPS_DIR not in p.relative_to(d).parts
                what = file_hash(p) if main else f"{st.st_size}\0{st.st_mtime_ns}"
            except OSError:
                continue
            h.update(f"{name_key(p.relative_to(d))}\0{what}\n".encode())
    return h.hexdigest()


def load_daily_state() -> dict:
    try:
        return json.loads(DAILY_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_daily_state(state: dict):
    try:
        DAILY_STATE.parent.mkdir(parents=True, exist_ok=True)
        DAILY_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def step_stage(ui: UI, roots: list[Path], smb: SMB | None, local_dest: Path | None,
               temp_dir: Path | None, dry: bool, title: str, new_state: dict | None = None,
               no_video: bool = False, no_workcopy: bool = True) -> list[Path]:
    """Кладёт во временную папку только то, что надо отправить на сервер.

    Оригиналы не меняются; сжатие/удаление/архивация дальше идут по копии.
    WAV, для которых на сервере уже есть WAV/FLAC, не копируются и не пережимаются.
    """
    dest = smb.ensure() if smb else local_dest
    assert dest is not None
    plan: list[tuple[Path, Path]] = []
    dirs: list[Path] = []
    staged_roots: list[Path] = []
    state = load_daily_state()
    for root in roots:
        base = staging_base(root, temp_dir)
        staged = base / root.name
        staged_roots.append(staged)
        sessions = set(find_sessions([root]))
        # отпечатки: что не менялось с прошлого удачного --daily, на сервере не сверяется
        prints = {name_key(s.relative_to(root)): fingerprint(s) for s in sessions}
        loose_print = fingerprint(root, sessions)
        old = state.get(str(root), {})
        opts = {"no_video": no_video, "no_workcopy": no_workcopy}
        fresh = (old.get("dest") == str(dest / root.name) and old.get("opts") == opts
                 and time.time() - old.get("full", 0) < DAILY_FULL_EVERY)
        same = {s for s in sessions if fresh and old.get("sessions", {}).get(name_key(s.relative_to(root))) == prints[name_key(s.relative_to(root))]}
        if new_state is not None:
            new_state[str(root)] = {"dest": str(dest / root.name), "opts": opts,
                                    "sessions": prints, "loose": loose_print,
                                    "full": old.get("full", 0) if fresh else time.time(),
                                    "dest_bytes": old.get("dest_bytes")}
        if same == sessions and fresh and old.get("loose") == loose_print:
            ui.info(f"{title}: {root.name} — без изменений с прошлого раза, сервер не сверяется")
            continue
        ui.header("Сверка с сервером:", root.name)  # долгий этап — окну видно, что идёт
        remote = scan_remote(dest / root.name)
        # сессии без изменений не копируются и не бэкапятся вовсе
        skip: set[Path] = set(same)  # не менялись с прошлого раза — без сверки
        sessions -= same
        if sessions:
            bar = ui.bar(f"{title}: сверка с сервером", len(sessions))
            for sess in sorted(sessions):
                if session_unchanged(sess, root, dest / root.name, remote, no_video, no_workcopy, not dry):
                    skip.add(sess)
                bar.advance()
            bar.close(f"без изменений: {len(skip - same)} из {len(sessions)}")
            for sess in sorted(skip):
                ui.info(f"  = {sess.name}: без изменений, пропущена")
        for d, filenames in walk_pruned(root, skip):
            dirs.append(staged / d.relative_to(root))
            for f in filenames:
                src = d / f
                rel = src.relative_to(root)
                r = remote.get(name_key(rel))
                if f == WAVECACHE and d in sessions:
                    continue
                if rel.suffix == PTREF_EXT and name_key(rel) in remote:
                    continue  # копия серверной ссылки — главная на сервере
                if BACKUPS_DIR in rel.parts or rel.suffix.lower() in SESSION_EXTS \
                        or f.startswith(BACKUPS_DIR):  # прежние архивы бэкапов — для слияния
                    pass
                elif is_audio(rel):
                    if audio_present(rel, remote) and not audio_changed(src, rel, remote, dest / root.name, not dry):
                        continue
                elif r and r.st_size == src.lstat().st_size \
                        and r.st_mtime >= src.lstat().st_mtime - MTIME_WINDOW:
                    if r.st_mtime > src.lstat().st_mtime + MTIME_WINDOW and not dry:
                        sync_mtime(src, dest / root.name / rel, r)  # сбитая дата на сервере, файл тот же
                    continue
                plan.append((src, staged / rel))

    size = sum(s.lstat().st_size for s, _ in plan)
    if dry:
        for s, _ in plan:
            ui.debug(f"во временную копию: {s}")
        ui.info(f"{title}: будет подготовлено {len(plan)} файлов ({fmt_bytes(size)})")
        return staged_roots

    for staged in staged_roots:
        if staged.exists():
            shutil.rmtree(staged)  # остаток прерванного запуска
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    ui.header("Временная копия:", f"{len(plan)} файлов")
    bar = ui.bar(title, size, "bytes")
    for s, t in plan:
        clone_file(s, t)
        bar.advance(s.lstat().st_size)
    bar.close(f"файлов: {len(plan)}")
    return staged_roots


def cleanup_staging(ui: UI, staged_roots: list[Path]):
    for staged in staged_roots:
        if staged.exists():
            ui.debug(f"удаление временной копии {staged}")
            shutil.rmtree(staged, ignore_errors=True)
        try:
            staged.parent.rmdir()  # только если пустая
        except OSError:
            pass


# ------------------------------------------------ сверка и удаление копии --

RESTORED_FILE = ".pt_restored.json"  # пишет pt_restore: откуда распакован проект и какие WAV — распаковка
JUNK = {".DS_Store", RESTORED_FILE}  # служебное: не данные проекта, при сверке и удалении не учитывается


def read_restored(root: Path) -> dict:
    """Запись pt_restore в корне проекта: {"origin": путь на сервере, "files": {отн. путь WAV: [размер, mtime]}}."""
    try:
        return json.loads((root / RESTORED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def restored_unchanged(root: Path, info: dict, f: Path) -> bool:
    """WAV получен распаковкой и с тех пор не менялся (размер и дата те же)."""
    rec = info.get("files", {}).get(name_key(f.relative_to(root)))
    try:
        st = f.stat()
    except OSError:
        return False
    return bool(rec) and [st.st_size, int(st.st_mtime)] == rec


def local_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if not p.is_dir() or p.is_symlink())


def same_content(a: Path, b: Path, bar: Bar | None = None, chunk: int = 8 << 20) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            da, db = fa.read(chunk), fb.read(chunk)
            if da != db:
                return False
            if not da:
                return True
            if bar:
                bar.advance(len(da))


def wav_size_from_flac(f: Path) -> int | None:
    """Размер исходного WAV, из которого сделан FLAC (из .ptchunks или из
    сохранённого --keep-foreign-metadata заголовка RIFF)."""
    side = f.with_suffix(CHUNKS_EXT)
    try:
        if side.exists():
            with _quiet_close(side) as fh:
                return CHUNKS_HEAD.unpack(fh.read(CHUNKS_HEAD.size))[3]
        riff = flac_meta(f)["riff"]
    except (OSError, ValueError, struct.error):
        return None
    if riff and riff[0][:4] == b"RIFF":
        return struct.unpack("<I", riff[0][4:8])[0] + 8
    return None


def same_audio(flac_path: Path, wav_path: Path, checksum: bool) -> bool:
    """FLAC и WAV — один и тот же файл: совпадает размер исходного WAV, а с
    checksum ещё и MD5 звука (flac записывает его при сжатии)."""
    size = wav_size_from_flac(flac_path)
    if size is None or wav_path.stat().st_size != size:
        return False
    if not checksum:
        return True
    m = flac_meta(flac_path)
    bps, md5 = m["bps"], m["md5"]
    if not any(md5):  # MD5 не записан — остаётся сверка по размеру
        return True
    info = wave_info(wav_path)
    if not info["audio_len"]:
        return False
    h = hashlib.md5()
    flip = bytes(x ^ 0x80 for x in range(256)) if bps <= 8 else None  # 8 бит WAV беззнаковые
    with open(wav_path, "rb") as fh:
        fh.seek(info["audio_off"])
        left = info["audio_len"]
        while left:
            chunk = fh.read(min(left, 8 << 20))
            if not chunk:
                return False
            h.update(chunk.translate(flip) if flip else chunk)
            left -= len(chunk)
    return h.digest() == md5


def audio_twin(p: Path) -> list[Path]:
    """Тот же звук под другим расширением: X.flac <-> X.wav/X.WAV."""
    if p.suffix.lower() == ".flac":
        return [p.with_suffix(".wav"), p.with_suffix(".WAV"), p.with_suffix(PTREF_EXT)]
    if p.suffix.lower() == ".wav":
        return [p.with_suffix(".flac"), p.with_suffix(PTREF_EXT)]
    if p.suffix == CHUNKS_EXT:
        return [p.with_suffix(".wav"), p.with_suffix(".WAV"), p.with_suffix(PTREF_EXT)]
    if is_audio(p):
        return [p.with_suffix(PTREF_EXT)]
    return []


def wav_matches_flac(flac: Path, wav: Path, checksum: bool) -> bool:
    """Локальный WAV — это и есть звук FLAC на сервере (распакован из него и не менялся).
    У FLAC с блоками > 16 МБ размер исходного WAV — в .ptchunks рядом."""
    side = flac.with_suffix(CHUNKS_EXT)
    if side.exists():
        return chunks_match_wav(side, wav, checksum)
    return same_audio(flac, wav, checksum)


def verify_ref(local: Path, stub: Path, checksum: bool) -> bool:
    """Ссылка на сервере описывает именно этот локальный файл, и оригинал на месте."""
    h, head, tail = read_stub(stub)
    if not stub_target(stub, h).exists():
        return False
    if h["kind"] == "same" and local.suffix.lower() == ".wav" and h.get("name", "").lower().endswith(".flac"):
        # оригинал на сервере — FLAC, а локально дубликат уже распакован в WAV
        return wav_matches_flac(stub_target(stub, h), local, checksum)
    if h["kind"] == "same":
        return (local.stat().st_size == h["size"]
                and (not checksum or file_hash(local) == h["sha256"]))
    if local.suffix == CHUNKS_EXT:  # блоки FLAC-копии целиком лежат в ссылке
        with open(local, "rb") as fh:
            _, off, _, size, _ = CHUNKS_HEAD.unpack(fh.read(CHUNKS_HEAD.size))
            return size == h["wav_size"] and fh.read(off) == head and fh.read() == tail
    if local.suffix.lower() == ".flac":
        m = flac_meta(local)
        return m["md5"].hex() == h["md5"] and flac_header(local, m) == (head, tail, h["wav_size"])
    if local.suffix.lower() == ".wav":  # уже распакован локально
        size = local.stat().st_size
        if size != h["wav_size"]:
            return False
        with open(local, "rb") as fh:
            if fh.read(len(head)) != head:
                return False
            fh.seek(size - len(tail))
            if fh.read() != tail:
                return False
            if not checksum:
                return True
            fh.seek(len(head))
            md5 = hashlib.md5()
            flip = bytes(x ^ 0x80 for x in range(256)) if h["bps"] <= 8 else None
            left = size - len(head) - len(tail)  # байт выравнивания уже входит в tail
            while left > 0:
                c = fh.read(min(left, 8 << 20))
                if not c:
                    return False
                md5.update(c.translate(flip) if flip else c)
                left -= len(c)
            return md5.hexdigest() == h["md5"]
    return False


def chunks_match_wav(side: Path, wav: Path, checksum: bool) -> bool:
    """.ptchunks описывает именно этот WAV: размер, а с checksum — SHA-256."""
    with _quiet_close(side) as fh:
        _, _, _, size, digest = CHUNKS_HEAD.unpack(fh.read(CHUNKS_HEAD.size))
    if wav.stat().st_size != size:
        return False
    return not checksum or file_hash(wav) == digest.hex()


def verify_root(ui: UI, root: Path, dest: Path, checksum: bool, bar: Bar) -> list[str]:
    """Сверка локальной папки с копией dest/<root.name>. Возвращает список расхождений."""
    bad: list[str] = []
    for src in local_files(root):
        rel = src.relative_to(root.parent)
        dst = dest / rel
        if src.name in JUNK or (src.suffix == PTREF_EXT and dst.exists()):
            # .DS_Store — вид окна Finder; локальная .ptref — копия серверной ссылки (та главнее)
            bar.advance(1 if not checksum else src.lstat().st_size)
            continue
        try:
            s, d = src.lstat(), dst.lstat()
            if src.is_symlink():
                ok = True  # rsync -a переносит ссылку как ссылку, содержимого нет
            else:
                ok = (s.st_size == d.st_size
                      and abs(s.st_mtime - d.st_mtime) <= MTIME_WINDOW)
                if ok and checksum:
                    ok = same_content(src, dst, bar)
                elif checksum:
                    bar.advance(s.st_size)
            if not checksum:
                bar.advance(1)
        except FileNotFoundError:
            ok = False
            # рабочая копия видео: на сервере вместо mov — запись в Workcopies.ptwork
            found = ptwork_entry_for(dst) if src.suffix.lower() == ".mov" else None
            if found:
                pw, data = found
                ok = (data.get("size") == src.stat().st_size and
                      (pw.parent / data["source"]).exists())
                ui.debug(f"{rel}: в {pw.name} — {'исходник на месте' if ok else 'не совпадает'}")
                bar.advance(1 if not checksum else src.lstat().st_size)
                if not ok:
                    bad.append(str(rel))
                continue
            # на сервере тот же звук в другом виде: WAV ушёл до того, как его сжали
            # локально (или наоборот) — бэкап считает их одним файлом и не дублирует
            ok = False
            twin = next((t for t in audio_twin(dst) if t.exists()), None)
            if twin:
                try:
                    if twin.suffix == PTREF_EXT:
                        ok = verify_ref(src, twin, checksum)
                    elif src.suffix == CHUNKS_EXT:
                        ok = chunks_match_wav(src, twin, checksum)
                    else:
                        if src.suffix.lower() == ".flac":
                            ok = same_audio(src, twin, checksum)
                        else:  # локальный WAV (например, не менялся после распаковки)
                            ok = wav_matches_flac(twin, src, checksum)
                    ui.debug(f"{rel}: на сервере {twin.name} — {'тот же звук' if ok else 'не совпадает'}")
                except (OSError, ValueError, struct.error) as e:
                    ui.debug(f"{rel}: сверка с {twin.name}: {e}")
            else:
                ui.debug(f"{rel}: нет на сервере")
            bar.advance(1 if not checksum else src.lstat().st_size)
        except OSError as e:
            ok = False
            ui.debug(f"{rel}: {os.strerror(e.errno) if e.errno else e}")
        if not ok:
            bad.append(str(rel))
    return bad


def snapshot(files: list[Path]) -> dict[Path, tuple[int, int]]:
    """Размер и дата каждого файла — чтобы удалять только то, что не менялось после сверки."""
    out = {}
    for p in files:
        try:
            st = p.lstat()
            out[p] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    return out


def remove_tree(ui: UI, root: Path, bar: Bar, verified: dict[Path, tuple[int, int]]) -> int:
    """Удаляет только файлы из снимка, сделанного до сверки, и только если они с тех пор
    не изменились: новый дубль или пересохранённый .ptx (сессия открыта) остаются."""
    errors = kept = 0
    for p, (size, mtime) in verified.items():
        try:
            st = p.lstat()
            if (st.st_size, st.st_mtime_ns) != (size, mtime):
                kept += 1
                ui.warn(f"изменился после сверки, не удалён: {p}")
            else:
                p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            errors += 1
            ui.error(f"{p}: {e}")
        bar.advance()
    for dirpath, _, filenames in os.walk(root, topdown=False):
        d = Path(dirpath)
        for f in filenames:  # появившийся после бэкапа .DS_Store не мешает удалению
            if f in JUNK:
                (d / f).unlink(missing_ok=True)
        try:
            d.rmdir()
        except OSError as e:
            if e.errno != errno.ENOTEMPTY:
                errors += 1
                ui.error(f"{d}: {e}")
            else:
                ui.warn(f"не удалена (появились новые файлы): {d}")
    return errors


def confirm(ui: UI, question: str) -> bool:
    if ui.gui:  # окно PT Backup показывает вопрос и отвечает строкой y/n в stdin
        ui.emit({"t": "ask", "text": question})
        answer = sys.stdin.readline().strip().lower()
        ui.log(f"{question} → {answer or 'нет ответа'}")
        return answer in ("y", "yes")
    if not sys.stdin.isatty():
        return False
    with ui.lock:
        try:
            return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes", "д", "да")
        except EOFError:
            return False


def step_delete(ui: UI, roots: list[Path], smb: SMB | None, local_dest: Path | None,
                checksum: bool, assume_yes: bool, dry: bool, title: str) -> int:
    files = {r: local_files(r) for r in roots}
    # снимок ДО сверки: удаляться будет только он; появившееся или изменившееся позже —
    # не проверено и не трогается
    snaps = {r: snapshot(f) for r, f in files.items()}
    count = sum(len(f) for f in files.values())
    size = sum(p.lstat().st_size for f in files.values() for p in f)
    if dry:
        for r in roots:
            ui.debug(f"rm -r {r} (после сверки с сервером)")
        ui.info(f"{title}: после сверки будет удалено {count} файлов ({fmt_bytes(size)})")
        return 0

    dest = smb.ensure() if smb else local_dest
    assert dest is not None
    mode = "побайтовая" if checksum else "размер + дата"
    bar = ui.bar(f"{title}: сверка ({mode})", size if checksum else count,
                 "bytes" if checksum else "count")
    verified: list[Path] = []
    errors = 0
    for r in roots:
        bad = verify_root(ui, r, dest, checksum, bar)
        if bad:
            errors += 1
            ui.error(f"{r.name}: не совпадает с сервером ({len(bad)} файлов), "
                     f"папка не удаляется")
            for b in bad[:20]:
                ui.debug(f"≠ {b}")
        else:
            verified.append(r)
    bar.set(bar.total)
    bar.close("готово" if not errors else f"расхождений в папках: {errors}")
    if not verified:
        return errors

    n = sum(len(files[r]) for r in verified)
    if not assume_yes:
        names = ", ".join(r.name for r in verified)
        if not confirm(ui, f"Удалить локально {n} файлов из: {names}?"):
            ui.warn("удаление отменено (для запуска без вопроса: --yes)")
            return errors
    bar = ui.bar(f"{title}: удаление", n)
    for r in verified:
        errors += remove_tree(ui, r, bar, snaps[r])
    bar.close("готово" if not errors else f"ошибок: {errors}")
    return errors


# -------------------------------------------------------------------- main --

DEST_HISTORY = Path.home() / "Library" / "Application Support" / "pt_backup" / "destinations.json"
DEST_HISTORY_MAX = 8


def load_dest_history() -> list[str]:
    """Недавние пути назначения; если истории ещё нет — из лога прошлых запусков."""
    try:
        return json.loads(DEST_HISTORY.read_text(encoding="utf-8"))[:DEST_HISTORY_MAX]
    except (OSError, ValueError):
        pass
    found: list[str] = []
    try:
        for line in reversed(default_log("pt_backup").read_text(encoding="utf-8").splitlines()):
            m = re.search(r"\] Назначение: (/Volumes/.+)$", line)
            if m and m.group(1) not in found and ".pt_backup_test" not in m.group(1):
                found.append(m.group(1))
            if len(found) >= DEST_HISTORY_MAX:
                break
    except OSError:
        pass
    return found


def save_dest_history(path: str):
    path = path.rstrip("/") or "/"
    hist = [path] + [h for h in load_dest_history() if h.rstrip("/") != path]
    try:
        DEST_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        DEST_HISTORY.write_text(json.dumps(hist[:DEST_HISTORY_MAX], ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except OSError:
        pass


RUN_HISTORY = DEST_HISTORY.with_name("runs.jsonl")


def record_run(args, ui: UI, started: float, rc: int, mode: str = "", dest: str = ""):
    """Строка в историю запусков (её показывает окно PT Backup). Пишется вместе с логом."""
    if args.no_log:
        return
    rec = {"start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
           "duration": round(time.time() - started),
           "paths": [str(Path(p).expanduser().resolve()) for p in args.paths],
           # pt_restore передаёт mode="restore" и dest — папку --to
           "dest": dest or os.path.expanduser(args.dest or DEFAULT_DEST),
           "mode": mode or ("daily" if args.daily else "archive" if args.delete_source else "backup"),
           "dry": args.dry_run, "rc": rc,
           "errors": ui.counts["error"], "warnings": ui.counts["warn"],
           "trigger": os.environ.get("PT_BACKUP_TRIGGER", ""),  # app / schedule / пусто
           "log": ui.log_at[0] if ui.log_at else "", "log_offset": ui.log_at[1] if ui.log_at else 0,
           # что дал бэкап; у распаковки и пробного прогона её нет
           "stats": None if mode == "restore" or args.dry_run else ui.stats}
    try:
        RUN_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def finder_tags(p: Path) -> set[str]:
    """Теги Finder папки (атрибут com.apple.metadata:_kMDItemUserTags: «Имя\\nцвет»)."""
    r = run_quiet(["/usr/bin/xattr", "-px", "com.apple.metadata:_kMDItemUserTags", str(p)], 10)
    if not r or r.returncode != 0:
        return set()
    try:
        return {t.split("\n")[0] for t in plistlib.loads(bytes.fromhex("".join(r.stdout.split())))}
    except (ValueError, plistlib.InvalidFileException):
        return set()


def tagged_folders(folder: Path, tags: set[str]) -> list[Path]:
    """Папка сама с тегом — она; иначе папки прямо внутри неё, у которых есть любой из тегов."""
    if finder_tags(folder) & tags:
        return [folder]
    return sorted(d for d in folder.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and finder_tags(d) & tags)


def tree_size(root: Path) -> int:
    """Размер папки (файлы и ссылки, без каталогов); недоступное пропускается."""
    total = 0
    for d, _, files in os.walk(root):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


def pick_folder(start: str | None) -> str | None:
    """Стандартное окно выбора папки macOS (None — отмена)."""
    script = 'set f to choose folder with prompt "Куда бэкапить"'
    if start and os.path.isdir(start):
        script += f' default location (POSIX file "{start}")'
    r = subprocess.run(["/usr/bin/osascript", "-e", script, "-e", "POSIX path of f"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def ask_dest(ui: UI, default: str) -> str:
    """Выбор пути назначения: недавние пути по номеру (Enter — последний),
    0 — окно выбора папки, или путь вручную / перетаскиванием из Finder."""
    options = load_dest_history()
    if default.rstrip("/") not in [o.rstrip("/") for o in options]:
        options.append(default.rstrip("/"))
    while True:
        lines = ["Куда бэкапить?"]
        for i, o in enumerate(options, 1):
            lines.append(f"  {i}) {o}" + ("   ← последний" if i == 1 and len(options) > 1 else ""))
        lines.append("  0) выбрать папку в Finder…")
        lines.append("Enter — 1, или номер, или путь (можно перетащить папку)")
        try:
            raw = input("\n".join(lines) + "\n> ").strip()
        except EOFError:
            raw = ""
        if raw in ("", *map(str, range(1, len(options) + 1))):
            path = options[int(raw or 1) - 1]
        elif raw.isdigit():
            if raw != "0":
                ui.error(f"нет пункта {raw}")
                continue
            path = pick_folder(options[0] if options else default)
            if not path:
                ui.warn("папка не выбрана")
                continue
        else:
            try:
                parts = shlex.split(raw)  # снимает кавычки и "\ " от перетаскивания
            except ValueError:
                parts = [raw]
            path = os.path.expanduser(" ".join(parts))
        if not path.startswith("/Volumes/") and not os.path.isdir(path):
            ui.error(f"не похоже на путь к папке на сервере: {path}")
            continue
        save_dest_history(path)
        ui.log(f"Путь назначения: {path}")
        return path


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Бэкап сессий Pro Tools на SMB-сервер.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Примеры:", 1)[1] if "Примеры:" in __doc__ else None)
    ap.add_argument("paths", nargs="*", type=Path,
                    help="папка сессии или проекта с несколькими сессиями")
    ap.add_argument("--dest", default=None,
                    help=f"путь назначения (по умолчанию: {DEFAULT_DEST}); без --dest "
                         f"обычный бэкап спрашивает путь в терминале, --daily — нет")
    ap.add_argument("--host", default=SMB_HOST)
    ap.add_argument("--user", default=SMB_USER)
    ap.add_argument("--password", default=SMB_PASS,
                    help="пароль SMB (по умолчанию — из связки ключей; или env PT_BACKUP_SMB_PASS)")
    ap.add_argument("--share", help="имя шары (по умолчанию из --dest: /Volumes/<share>/…)")
    ap.add_argument("--local", action="store_true",
                    help="--dest — обычная папка, без SMB")
    ap.add_argument("--retries", type=int, default=5, help="попыток переподключения")
    ap.add_argument("--retry-delay", type=float, default=10, help="пауза между попытками, с")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 4),
                    help="параллельных процессов flac (по умолчанию 8: остальные ядра — Pro Tools и rsync)")
    ap.add_argument("--skip", action="append", choices=STEPS, default=[],
                    help="пропустить шаг (можно несколько раз)")
    ap.add_argument("--delete-source", action="store_true",
                    help="после бэкапа сверить копию на сервере и удалить локальные папки")
    ap.add_argument("--verify", choices=("quick", "checksum"), default="quick",
                    help="сверка перед удалением: quick — размер и дата (быстро), "
                         "checksum — побайтовое сравнение с сервером (медленно)")
    ap.add_argument("--gui", action="store_true",
                    help="вывод событиями JSON для окна PT Backup")
    ap.add_argument("--list-dests", action="store_true",
                    help="напечатать недавние пути назначения (JSON) и выйти")
    ap.add_argument("--tag", action="append", default=[], metavar="ТЕГ",
                    help="бэкапить только папки с этим тегом Finder: сама папка или папки прямо внутри неё "
                         "(можно несколько --tag — подходит любой)")
    ap.add_argument("--no-video", action="store_true",
                    help="не отправлять на сервер видео вообще (mov, mp4 и т.п., в том числе вне проекта)")
    ap.add_argument("--no-workcopy", action="store_true",
                    help="не отправлять рабочие копии видео *_Workcopy_*.mov от mix_prep, если "
                         "рядом есть исходное видео (вместо mov — запись в Workcopies.ptwork; вернуть: "
                         "pt_restore --workcopy)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="не искать дубликаты аудио (отправлять все копии целиком)")
    ap.add_argument("--daily", action="store_true",
                    help="не трогать локальные файлы: сжатие и прочее — во временной копии")
    ap.add_argument("--temp-dir", type=Path,
                    help=f"папка для временной копии в --daily "
                         f"(по умолчанию <папка проекта>/../{STAGING_DIR})")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="не спрашивать подтверждение удаления")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="показать, что будет сделано, ничего не меняя")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    ap.add_argument("--plain", action="store_true",
                    help="построчный прогресс (автоматически, если вывод не в терминал)")
    ap.add_argument("--log", type=Path, default=default_log("pt_backup"),
                    help="лог-файл (по умолчанию ~/Library/Logs/pt_backup.log)")
    ap.add_argument("--no-log", action="store_true", help="не писать лог-файл")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    events = os.environ.get("PT_BACKUP_EVENTS")
    if events and not args.list_dests:
        # задание расписания: события --gui в файл, окно PT Backup показывает по нему прогресс
        sys.stdout = open(events, "w", encoding="utf-8", buffering=1)
        args.gui = True
        print(json.dumps({"t": "start", "pid": os.getpid()}))  # окно: жив ли процесс задания
    if args.list_dests:  # для окна PT Backup: недавние пути + путь по умолчанию
        hist = load_dest_history()
        if DEFAULT_DEST.rstrip("/") not in [h.rstrip("/") for h in hist]:
            hist.append(DEFAULT_DEST.rstrip("/"))
        print(json.dumps(hist, ensure_ascii=False))
        return 0
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except AttributeError:
        pass
    ui = UI(args.verbose, args.plain, None if args.no_log else args.log.expanduser(),
            gui=args.gui)
    if not args.paths:
        ui.error("укажите папку сессии или проекта")
        return 2
    started, rc = time.time(), 1
    try:
        rc = backup(args, ui)
        return rc
    except KeyboardInterrupt:
        rc = 130
        raise
    finally:
        record_run(args, ui, started, rc)
        if ui.gui:
            ui.emit({"t": "end", "rc": rc})


def backup(args, ui: UI) -> int:
    def on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    if (args.delete_source or args.daily) and "rsync" in args.skip:
        ui.error("--delete-source/--daily нельзя использовать с --skip rsync")
        return 2
    if args.delete_source and args.daily:
        ui.error("--daily не трогает локальные файлы и несовместим с --delete-source")
        return 2

    roots = [p.expanduser().resolve() for p in args.paths]
    for r in roots:
        vol = Path(*r.parts[:3])
        if not r.is_dir() and r.parts[1:2] == ("Volumes",) and not os.path.ismount(vol):
            # проект лежит на шаре сервера, которая сейчас не подключена — подключаем
            ui.info(f"Подключение {vol} (там лежит {r.name})")
            try:
                SMB(ui, args.host, args.user, args.password, r.parts[2], "",
                    args.retries, args.retry_delay).ensure()
            except ConnectionError as e:
                ui.error(str(e))
                return 3
        if not r.is_dir():
            ui.error(f"папка не найдена: {r}")
            return 2
    # один проект — один запуск: ночное задание и ручной бэкап одного проекта не пересекаются
    locks_dir = DAILY_STATE.parent / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    held = []

    def lock(r: Path) -> bool:
        fh = open(locks_dir / (hashlib.sha1(str(r).encode()).hexdigest()[:16] + ".lock"), "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        held.append(fh)  # держится до конца процесса
        return True

    if args.tag:  # отбор по тегам Finder — на момент запуска: новый проект с тегом попадёт сам
        tags = set(args.tag)
        roots = list(dict.fromkeys(t for r in roots for t in tagged_folders(r, tags)))
        ui.info(f"По тегам ({', '.join(sorted(tags))}): " + (", ".join(r.name for r in roots) or "нет папок"))
        if not roots:
            return 0
    busy = [r for r in roots if not lock(r)]
    for r in busy:
        ui.warn(f"{r.name}: сейчас бэкапится другим запуском — пропущен")
    roots = [r for r in roots if r not in busy]
    if not roots:
        return 0

    # SMB: проверяем заранее, чтобы не сжимать всё и не упасть на rsync
    smb = None
    local_dest = None
    if "rsync" not in args.skip and args.dest is None:
        # проект распакован pt_restore с сервера — бэкап туда же, откуда брали
        origins = {read_restored(r).get("origin") for r in roots}
        if len(origins) == 1 and None not in origins:
            args.dest = str(Path(origins.pop()).parent)
            ui.info(f"Куда: {args.dest} (откуда проект распаковывали)")
    if "rsync" not in args.skip:
        # без --dest и не в --daily — спрашиваем путь (если есть кого спросить)
        if args.dest is None and not args.daily and sys.stdin.isatty():
            args.dest = ask_dest(ui, DEFAULT_DEST)
        elif args.dest and not args.dry_run and not args.no_log:  # явный путь (в т.ч. из окна PT Backup); тесты идут с --no-log
            save_dest_history(os.path.expanduser(args.dest))
        dest = Path(os.path.expanduser(args.dest or DEFAULT_DEST))
        for r in roots:  # бэкап сам в себя: rsync писал бы в исходную папку проекта
            d = dest.resolve()
            if d / r.name == r or d == r or d.is_relative_to(r):
                ui.error(f"{r.name}: проект уже лежит в папке назначения {dest} — выберите другую папку")
                return 2
        if args.local or local_volume(dest):  # USB-диск и т.п. — как --local
            local_dest = dest
            ui.info(f"Назначение: {dest.resolve()}")
        else:
            smb = make_smb(ui, dest, args.host, args.user, args.password, args.share,
                           args.retries, args.retry_delay)
            if not smb:
                return 2
            if not smb.server_up():
                ui.error(f"сервер {args.host}:445 недоступен")
                return 3
            if not args.dry_run:
                try:
                    ui.info(f"Назначение: {smb.ensure()}")
                except ConnectionError as e:
                    ui.error(str(e))
                    return 3
            else:
                ui.info(f"Сервер {args.host} доступен; назначение: {smb.dest() or dest}")

    sessions = find_sessions(roots)
    ui.info(f"Найдено сессий: {len(sessions)}")
    server = None  # папка назначения (для статистики «было → стало»)
    if "rsync" not in args.skip and not args.dry_run:
        try:
            server = smb.ensure() if smb else local_dest
        except ConnectionError:
            server = None
    per_root: dict[str, dict] = {}  # по каждому проекту отдельно (в --daily отправляется копия с тем же именем)
    if server:
        per_root = {r.name: {"name": r.name, "existed": (server / r.name).is_dir(),
                             "src_bytes": tree_size(r), "files": 0, "bytes": 0} for r in roots}
        ui.stats["roots"] = list(per_root.values())  # те же словари: дальше дополняются на месте

    active = [s for s in STEPS if s not in args.skip]
    shown = [s for s in active if s != "wavecache"]  # WaveCache удаляется молча
    n = len(shown)
    titles = {
        "flac": "WAV → FLAC",
        "backups": "Session File Backups",
        "rsync": "rsync → сервер",
    }
    errors = 0
    rsync_errors = 0
    staged: list[Path] = []
    temp_dir = args.temp_dir.expanduser().resolve() if args.temp_dir else None
    jobs = max(1, args.jobs)
    try:
        work_roots = roots
        daily_state: dict = {}  # отпечатки для следующего --daily; сохраняются только после удачного
        if args.daily:
            # временная копия удаляется целиком (rmtree) — она не должна совпадать
            # с проектом или пересекаться с ним (например, --temp-dir = папка проекта)
            for r in roots:
                st_dir = (staging_base(r, temp_dir) / r.name).resolve()
                if st_dir == r or st_dir.is_relative_to(r) or r.is_relative_to(st_dir):
                    ui.error(f"временная копия {st_dir} совпадает с проектом {r} или "
                             f"пересекается с ним — укажите другую --temp-dir")
                    return 2
            staged = step_stage(ui, roots, smb, local_dest, temp_dir, args.dry_run,
                                "Временная копия", daily_state, args.no_video, args.no_workcopy)
            if args.dry_run:
                ui.info("Пробный прогон: остальные шаги выполняются над временной копией")
                staged = []
                return 0
            work_roots = staged

        # Конвейер: локальные шаги (FLAC, WaveCache, бэкапы) идут в своём потоке впереди,
        # отправка на сервер — здесь, по очереди, в порядке сессий. Пока сессия N уходит
        # на сервер, сессия N+1 уже сжимается.
        total = sum(len(find_sessions([r])) for r in work_roots)
        uploading = "rsync" in active
        local_steps = [s for s in active if s != "rsync"]
        ready: queue.Queue = queue.Queue()
        stop = threading.Event()

        def step_title(step: str, name: str) -> str:
            return f"  [{shown.index(step) + 1}/{n}] {titles[step]} · {name}" if step in shown else ""

        def produce():
            _lane.name = "local"
            k = 0
            try:
                for root in work_roots:
                    root_sessions = find_sessions([root])
                    restored = read_restored(root) if server else {}

                    def keep(w: Path, root=root, restored=restored) -> bool:
                        return (restored_unchanged(root, restored, w) and
                                any(t.exists() for t in audio_twin(server / w.relative_to(root.parent))))

                    packed: set[Path] = set()  # сессии, чьи архивы бэкапов слиты в .ptbak в этом запуске
                    for sess in root_sessions:
                        if stop.is_set():
                            return
                        k += 1
                        ui.header(f"Сессия {k}/{total}:", sess.name)
                        ui.debug(f"  {sess}")
                        errs = 0
                        for step in local_steps:
                            if step == "flac":
                                errs += step_flac(ui, [sess], jobs, args.dry_run, step_title(step, sess.name),
                                                  keep=keep if restored else None)
                            elif step == "wavecache":
                                step_wavecache(ui, [sess], args.dry_run)
                            elif step == "backups":
                                srv = None
                                if uploading and not args.dry_run:
                                    try:
                                        d0 = smb.ensure() if smb else local_dest
                                        srv = d0 / sess.relative_to(root.parent) if d0 else None
                                    except ConnectionError:
                                        srv = None
                                errs += step_backups(ui, [sess], args.dry_run, step_title(step, sess.name),
                                                     {sess: srv} if srv else None,
                                                     packed if srv else None)
                        ready.put(("session", root, sess, k, errs, sess in packed))
                    # файлы проекта вне сессий (Source и т.п.) — после сессий: оригиналами
                    # дубликатов становятся файлы сессий, копии в Source уходят ссылками
                    if uploading and has_loose_files(root, root_sessions) and not stop.is_set():
                        errs = 0
                        # WAV вне сессий сжимаются в любой папке (Source, PM, ME, …);
                        # pt_restore распакует их обратно, где бы ни лежали
                        wavs = loose_wavs(root, root_sessions) if "flac" in active else []
                        if restored:
                            wavs = [w for w in wavs if not keep(w)]
                        if wavs:
                            # пока они сжимаются, отправка не простаивает: сразу уходит всё вне
                            # сессий, кроме папок с WAV (там же пишутся временные *.part);
                            # эти папки — вторым проходом, уже с FLAC
                            busy = {w.parent for w in wavs}
                            if root not in busy:
                                ready.put(("loose", root, set(root_sessions) | busy, 0, 0, True))
                            ui.header("Прочие файлы:", root.name)
                            errs += step_flac(ui, [], jobs, args.dry_run,
                                              f"  {titles['flac']} · {root.name}", files=wavs)
                        ready.put(("loose", root, set(root_sessions), 0, errs, False))
            except BaseException as e:  # noqa: BLE001 — передаётся в главный поток
                ready.put(("error", e))
            finally:
                if ui.gui:  # окно: столбец «Локально» больше ничего не ждёт
                    ui.emit({"t": "lane", "lane": "local", "done": True})
                ready.put(None)

        worker = threading.Thread(target=produce, name="local", daemon=True)
        _lane.name = "upload"
        worker.start()
        dedups: dict[Path, Dedup | None] = {}
        try:
            while True:
                try:
                    item = ready.get(timeout=0.5)  # с таймаутом — чтобы Ctrl+C/SIGTERM доходили
                except queue.Empty:
                    continue
                if item is None:
                    break
                if item[0] == "error":
                    raise item[1]
                kind, root, what, k, errs, flag = item  # flag: prune у сессии / «первый проход» у прочих
                errors += errs
                if not uploading:
                    continue
                if root not in dedups:
                    dedups[root] = None if args.no_dedup else Dedup(ui, root)
                sent0 = (ui.stats["files"], ui.stats["bytes"])
                if kind == "session":
                    ui.header(f"Отправка {k}/{total}:", what.name)
                    e = step_rsync(ui, what, root.parent, set(), smb, local_dest,
                                   args.retries, args.retry_delay, args.dry_run,
                                   step_title("rsync", what.name), dedups[root], args.no_workcopy,
                                   prune=flag, no_video=args.no_video)
                else:
                    ui.header("Отправка:", f"прочие файлы {root.name}"
                              + (" (без папок, где ещё сжимаются WAV)" if flag else ""))
                    e = step_rsync(ui, root, root.parent, what, smb, local_dest,
                                   args.retries, args.retry_delay, args.dry_run,
                                   f"  {titles['rsync']} · {root.name}", dedups[root], args.no_workcopy,
                                   no_video=args.no_video)
                rsync_errors += e
                errors += e
                if root.name in per_root:
                    per_root[root.name]["files"] += ui.stats["files"] - sent0[0]
                    per_root[root.name]["bytes"] += ui.stats["bytes"] - sent0[1]
        finally:
            stop.set()
            if worker.is_alive():
                kill_all()  # прерывание: остановить flac/zstd локального потока
                worker.join(timeout=10)

        # видео сессий, лежащее вне папки проекта (например, /Volumes/…/Video) — тоже на сервер
        for r in (roots if server and uploading and not args.no_video else []):
            if ui.gui:  # окну этап виден сразу: поиск по .ptx тоже занимает время
                ui.header("Видео вне проекта:", r.name)
            vids = external_videos(ui, r)  # разобранные .ptx — из кэша ptx_videos.json
            if vids and not ui.gui:
                ui.header("Видео вне проекта:", r.name)
            if vids:
                e, n, nb = upload_external_videos(ui, r, vids, server, args.no_workcopy)
                errors += e
                rsync_errors += e
                if r.name in per_root:
                    per_root[r.name]["files"] += n
                    per_root[r.name]["bytes"] += nb

        for r in roots:
            st = per_root.get(r.name)
            if not st:
                continue
            ds = daily_state.get(str(r), {})
            # ничего не ушло, размер на сервере уже известен — обход шары по SMB не нужен
            st["dest_bytes"] = (ds["dest_bytes"] if not st["files"] and ds.get("dest_bytes") is not None
                                else tree_size(server / st["name"]))
            if ds:
                ds["dest_bytes"] = st["dest_bytes"]
            ui.info(f"{st['name']}: "
                    + ("обновление" if st["existed"] else "первый бэкап, папки на сервере не было")
                    + (f"; отправлено файлов: {st['files']} ({fmt_bytes(st['bytes'])}); " if st["files"]
                       else "; ничего не отправлено; ")
                    + f"проект {fmt_bytes(st['src_bytes'])} → на сервере {fmt_bytes(st['dest_bytes'])}")

        if args.daily and daily_state and not errors and not args.no_log:
            save_daily_state({**load_daily_state(), **daily_state})

        if args.delete_source:
            title = "▸ Удаление локальной копии"
            if rsync_errors:
                ui.warn(f"{title}: пропущено — rsync завершился с ошибкой")
            else:
                errors += step_delete(ui, roots, smb, local_dest,
                                      args.verify == "checksum", args.yes,
                                      args.dry_run, title)
    except KeyboardInterrupt:
        kill_all()
        for b in list(ui.bars):
            b.close("прервано")
        ui.error("прервано пользователем")
        return 130
    except ConnectionError as e:
        for b in list(ui.bars):
            b.close("ошибка")
        ui.error(str(e))
        return 3
    finally:
        cleanup_staging(ui, staged)

    ui.info("Готово." if not errors else ui.red(f"Завершено с ошибками: {errors}"))
    return 0 if not errors else 1


if __name__ == "__main__":
    run_main(main)
