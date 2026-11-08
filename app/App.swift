// PT Backup — окно для pt_backup.py: перетащить проект/сессию, выбрать куда и как бэкапить.
// Движок — установленный /usr/local/bin/pt_backup.py (тот же, что у сервисов Finder);
// он запускается с --gui и присылает события строками JSON (сессия, шаг, байты, скорость).
import SwiftUI
import AppKit
import UniformTypeIdentifiers
import UserNotifications

let scriptPath = "/usr/local/bin/pt_backup.py"
let pythonPath: String = {
    let pyenv = NSHomeDirectory() + "/.pyenv/versions/3.12.2/bin/python3"
    return FileManager.default.isExecutableFile(atPath: pyenv) ? pyenv : "/usr/bin/python3"
}()
let logPath = NSHomeDirectory() + "/Library/Logs/pt_backup.log"
let restoreScript = "/usr/local/bin/pt_restore.py"
let restoreLogPath = NSHomeDirectory() + "/Library/Logs/pt_restore.log"
let supportDir = NSHomeDirectory() + "/Library/Application Support/pt_backup"
let runsPath = supportDir + "/runs.jsonl"          // пишет сам pt_backup.py, в т.ч. из сервисов и по расписанию
let schedulePath = supportDir + "/schedule.json"
let agentLabel = "local.pavel.pt-backup.daily"
let agentPath = NSHomeDirectory() + "/Library/LaunchAgents/\(agentLabel).plist"
let scheduleEventsPath = supportDir + "/schedule_events.jsonl"   // pt_backup пишет сюда события ночного задания
// из Finder/launchd приходит урезанный PATH — flac/rsync/zstd из Homebrew
let enginePath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

@discardableResult
func launchctl(_ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    p.arguments = args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

func openLog(_ path: String = logPath) {
    NSWorkspace.shared.open([URL(fileURLWithPath: path)],
                            withApplicationAt: URL(fileURLWithPath: "/System/Applications/Utilities/Console.app"),
                            configuration: NSWorkspace.OpenConfiguration())
}

enum Job { case backup, restore }

/// Ход конвейера по событиям pt_backup --gui (ручной запуск и ночное задание).
struct PipeState {
    var phase = ""   // этап до сессий: «Сверка с сервером · Cyber Crush», «Временная копия · 928 файлов»
    var local = Lane(), upload = Lane()
    var uploadIndex = 0, localIndex = 0, sessionTotal = 0
    var uploaded = 0   // сессий отправлено целиком (uploadIndex — какая отправляется сейчас)
    var steps: [Step] = []

    /// Применяет событие header/bar/lane; false — событие не про ход работы.
    mutating func apply(_ e: [String: Any]) -> Bool {
        switch e["t"] as? String ?? "" {
        case "header":
            let label = e["label"] as? String ?? ""
            let name = e["name"] as? String ?? ""
            let isUpload = e["lane"] as? String == "upload"
            let loose = label.hasPrefix("Прочие") || name.hasPrefix("прочие")
            var stage = label
            if let r = label.range(of: #"(\d+)/(\d+)"#, options: .regularExpression) {
                let n = label[r].split(separator: "/")
                stage = "Сессия \(n[0]) из \(n[1])"
            } else if label.hasPrefix("Копирование") || label.hasPrefix("На месте") {
                stage = label
            } else if label.hasPrefix("Сверка") || label.hasPrefix("Временная") {
                phase = label + " · " + name
            }
            // файлы вне сессий подписываются «Прочие файлы», а проект — мелко справа
            var shownName = name
            if label.hasPrefix("Видео вне проекта") {
                shownName = "Видео вне проекта"; stage = name
            } else if loose {
                let project = name.hasPrefix("прочие файлы ") ? String(name.dropFirst("прочие файлы ".count)) : name
                shownName = project.contains("(без папок") ? "Прочие файлы, без папок с WAV" : "Прочие файлы"
                stage = "Source и т.п. · " + (project.components(separatedBy: " (без").first ?? project)
            }
            if isUpload { upload.session = shownName; upload.stage = stage } else { local.session = shownName; local.stage = stage }
            // «Сессия 3/9» (локально) / «Отправка 2/9»
            if let r = label.range(of: #"(\d+)/(\d+)"#, options: .regularExpression) {
                let n = label[r].split(separator: "/").compactMap { Int($0) }
                if n.count == 2 {
                    sessionTotal = n[1]
                    if isUpload { uploadIndex = n[0] } else { localIndex = n[0] }
                }
            }
        case "bar":
            let title = e["title"] as? String ?? ""
            let value = e["value"] as? Double ?? 0, total = e["total"] as? Double ?? 0
            let unit = e["unit"] as? String ?? "", rate = e["rate"] as? String ?? ""
            var lane = e["lane"] as? String == "upload" ? upload : local
            if e["done"] as? Bool == true {
                let status = e["status"] as? String ?? ""
                let bad = status.hasPrefix("ошиб") || status.hasPrefix("расхожд") || status.hasPrefix("передача зависла")
                steps.append(Step(title: title, status: status, bad: bad, upload: e["lane"] as? String == "upload"))
                lane.title = ""; lane.fraction = 0; lane.detail = ""
                // отправка закончила и ждёт сжатия следующей — не держать имя отправленной
                if e["lane"] as? String == "upload" && !title.hasSuffix(": сравнение") {
                    if title.hasPrefix("[") { uploaded = max(uploaded, uploadIndex) }  // «[3/3] rsync → сервер · S»
                    lane.session = ""; lane.stage = ""
                }
            } else {
                // «[1/3] WAV → FLAC · S2» → «WAV → FLAC»: номер шага и сессия видны и так
                lane.title = title.replacingOccurrences(of: #"^\[\d+/\d+\] "#, with: "", options: .regularExpression)
                    .components(separatedBy: " · ").first ?? title
                lane.fraction = total > 0 ? min(1, value / total) : 0
                if unit == "bytes" {
                    let f = ByteCountFormatter()
                    f.allowsNonnumericFormatting = false  // «0 КБ», а не «Zero KB»
                    lane.detail = "\(f.string(fromByteCount: Int64(value))) из \(f.string(fromByteCount: Int64(total)))"
                        + (rate.isEmpty ? "" : " · \(rate)")
                } else if unit == "count" {
                    lane.detail = "\(Int(value)) из \(Int(total))"
                } else { lane.detail = "\(Int(lane.fraction * 100))%" }
            }
            if e["lane"] as? String == "upload" { upload = lane } else { local = lane }
        case "lane":   // локальный поток всё сжал — больше в «Локально» ничего не будет
            if e["lane"] as? String == "local" { local = Lane(finished: true) }
        default: return false
        }
        return true
    }

    func overall(restore: Bool) -> Double {
        if restore {
            guard sessionTotal > 0, localIndex > 0 else { return 0 }
            return min(1, (Double(localIndex - 1) + local.fraction * 0.99) / Double(sessionTotal))
        }
        guard sessionTotal > 0 else { return 0 }
        let current = uploadIndex > uploaded ? upload.fraction * 0.99 : 0
        return min(1, (Double(uploaded) + current) / Double(sessionTotal))
    }

    var title: String {
        if sessionTotal > 0 && uploaded == sessionTotal && !upload.session.isEmpty {
            return "Сессии отправлены · \(upload.session)"     // дальше прочие файлы, видео вне проекта
        }
        if sessionTotal > 0 { return "Отправлено \(uploaded) из \(sessionTotal)" }
        return phase.isEmpty ? "Подготовка…" : phase
    }
}

/// Ход ночного задания: читает файл событий, который pt_backup пишет при PT_BACKUP_EVENTS.
@MainActor
final class ScheduleWatch: ObservableObject {
    @Published var pipe = PipeState()
    @Published var running = false
    private var offset: UInt64 = 0
    private var pid: Int32 = 0
    private var timer: Timer?

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }

    func poll() {
        guard let h = FileHandle(forReadingAtPath: scheduleEventsPath) else { running = false; return }
        defer { try? h.close() }
        let size = (try? h.seekToEnd()) ?? 0
        if size < offset { offset = 0; pipe = PipeState() }   // новый запуск переписал файл
        try? h.seek(toOffset: offset)
        let data = h.readDataToEndOfFile()
        guard let lastNL = data.lastIndex(of: 0x0A) else { return finishCheck() }
        offset += UInt64(lastNL + 1)   // недописанную строку прочитаем в следующий раз
        for line in data[..<lastNL].split(separator: 0x0A) {
            guard let e = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any] else { continue }
            switch e["t"] as? String {
            case "start": pipe = PipeState(); pid = Int32(e["pid"] as? Int ?? 0); running = true
            case "end": running = false
            default: _ = pipe.apply(e)
            }
        }
        finishCheck()
    }

    /// Процесс задания погиб без события end (сон, сбой) — прогресс не держим.
    private func finishCheck() {
        if running && pid > 0 && kill(pid, 0) != 0 { running = false }
    }
}

/// Завершённое задание текущей пачки (запуск + всё, что встало за ним в очередь).
struct Finished: Identifiable {
    let id = UUID()
    let job: Job, mode: Mode
    let sources: [URL], dest: String
    let code: Int32
    let run: Run?          // запись истории: ошибки, предупреждения, лог
    let args: [String]
    var ok: Bool { code == 0 }
}

/// Задание, ждущее своей очереди: запускается, когда закончится текущее.
struct Queued: Identifiable {
    let id = UUID()
    let job: Job, mode: Mode
    let sources: [URL], dest: String
    let args: [String]
}

enum Mode: String, CaseIterable, Identifiable {
    case backup = "Бэкап", archive = "Архив"
    var id: String { rawValue }
    var note: String {
        switch self {
        case .backup: return "WAV сжимаются во FLAC прямо в папке проекта, всё новое уходит на сервер."
        case .archive: return "Бэкап, сверка с сервером и удаление локальной папки."
        }
    }
}

/// Строка из runs.jsonl.
struct Run: Identifiable, Decodable {
    var id: String { start + paths.joined() }
    let start: String, duration: Int, paths: [String], dest: String, mode: String
    let dry: Bool, rc: Int, errors: Int, warnings: Int, trigger: String
    let log: String?, log_offset: Int?   // нет у записей до 2026-10-25
    let stats: RunStats?
    var date: Date? { ISO8601DateFormatter.local.date(from: start) }

    /// Кусок общего лога от строки «запуск» до «завершено» этого запуска.
    func logText() -> String? {
        guard let log, let off = log_offset else { return nil }
        for path in [log, log + ".1"] {   // .1 — лог, отложенный при ротации
            guard let h = FileHandle(forReadingAtPath: path) else { continue }
            defer { try? h.close() }
            guard (try? h.seek(toOffset: UInt64(off))) != nil,
                  let data = try? h.read(upToCount: 4 << 20) else { continue }
            let lines = String(decoding: data, as: UTF8.self).split(separator: "\n", omittingEmptySubsequences: false)
            guard lines.first?.contains("============ запуск") == true else { continue }
            var out: [Substring] = [lines[0]]
            for l in lines.dropFirst() {
                if l.contains("============ запуск") { break }
                out.append(l)
                if l.contains("============ завершено") { break }
            }
            return out.joined(separator: "\n")
        }
        return nil
    }
}

/// Что дал бэкап — по каждому проекту отдельно (у записей с 2026-10-25).
struct RunStats: Decodable {
    let roots: [RootStats]?

    /// Все поля необязательные: неполная запись не должна выбрасывать запуск из истории.
    struct RootStats: Decodable {
        let name: String?
        let existed: Bool?
        let files: Int?
        let bytes: Double?
        let src_bytes: Int64?, dest_bytes: Int64?

        var text: String {
            let f = ByteCountFormatter()
            let n = files ?? 0
            var parts = [existed == false ? "первый бэкап, на сервере папки не было"
                         : n == 0 ? "ничего не отправлено" : "отправлено файлов: \(n) (\(f.string(fromByteCount: Int64(bytes ?? 0))))"]
            if let a = src_bytes, let b = dest_bytes {
                parts.append("проект \(f.string(fromByteCount: a)) → на сервере \(f.string(fromByteCount: b))")
            }
            return parts.joined(separator: " · ")
        }
    }

    /// Одна строка на проект; имя — только если проектов несколько.
    var lines: [String] {
        let rs = roots ?? []
        return rs.map { (rs.count > 1 ? ($0.name ?? "") + ": " : "") + $0.text }
    }
}

/// /Volumes/Storage A/Sessions_2/_nda → «Storage A › Sessions_2 › _nda».
func prettyPath(_ d: String) -> String {
    var c = Array((d as NSString).pathComponents.dropFirst())
    if c.first == "Volumes" { c.removeFirst() }
    return c.joined(separator: " › ")
}

extension ISO8601DateFormatter {
    static let local: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withFullDate, .withTime, .withColonSeparatorInTime, .withDashSeparatorInDate]
        f.timeZone = .current
        return f
    }()
}

/// Расписание daily: хранится в schedule.json, по нему пишется LaunchAgent.
struct Schedule: Codable, Equatable {
    var hour = 3, minute = 0
    var days: [Int] = Array(1...7)   // всегда каждый день (выбор дней убран); поле — для старых schedule.json
    var paths: [String] = []
    var dest = ""
    var tags: [String]? = nil        // теги Finder: бэкапятся только папки с ними (сами или внутри paths)
    var noWorkcopy: Bool? = nil      // nil = да: рабочие копии mov не отправляются
    var noVideo: Bool? = nil         // true: видео не отправляется вообще
}

/// Теги Finder папки.
func finderTags(_ path: String) -> Set<String> {
    Set((try? URL(fileURLWithPath: path).resourceValues(forKeys: [.tagNamesKey]).tagNames) ?? nil ?? [])
}

/// Что будет бэкапиться из папки при отборе по тегам — как tagged_folders в pt_backup:
/// папка сама с тегом — она; иначе папки прямо внутри неё с любым из тегов.
func taggedFolders(_ path: String, tags: Set<String>) -> [String] {
    if !finderTags(path).isDisjoint(with: tags) { return [path] }
    let kids = (try? FileManager.default.contentsOfDirectory(atPath: path)) ?? []
    return kids.filter { !$0.hasPrefix(".") }.sorted()
        .map { (path as NSString).appendingPathComponent($0) }
        .filter { p in
            var dir: ObjCBool = false
            return FileManager.default.fileExists(atPath: p, isDirectory: &dir) && dir.boolValue
                && !finderTags(p).isDisjoint(with: tags)
        }
}

/// Теги, из которых выбирать: избранные теги Finder, плюс стоящие на самих
/// папках и на папках прямо внутри них.
func availableTags(_ paths: [String]) -> [String] {
    // избранные теги боковой панели Finder (в них и цвета — под теми именами, что у тебя в Finder)
    var all = Set(UserDefaults(suiteName: "com.apple.finder")?.stringArray(forKey: "FavoriteTagNames") ?? [])
    all.remove("")
    for p in paths {
        all.formUnion(finderTags(p))
        for k in (try? FileManager.default.contentsOfDirectory(atPath: p)) ?? [] where !k.hasPrefix(".") {
            all.formUnion(finderTags((p as NSString).appendingPathComponent(k)))
        }
    }
    return all.sorted()
}

/// Одна полоса конвейера: локальная обработка или отправка на сервер.
struct Lane {
    var finished = false                // эта часть конвейера всё сделала (событие "lane" от движка)
    var stage = ""                      // «Сессия 3 из 9» / «Вне сессий (Source и т.п.)»
    var session = "", title = "", detail = ""
    var fraction = 0.0
}

struct Step: Identifiable { let id = UUID(); let title: String; let status: String; let bad: Bool; var upload = false }
struct LogLine: Identifiable { let id = UUID(); let level: String; let text: String }

@MainActor
final class Model: ObservableObject {
    @Published var sources: [URL] = []
    @Published var runSources: [URL] = []   // что в работе/было в последнем прогоне: список «Что бэкапить» после запуска пустеет
    @Published var runDest = ""
    @Published var job = Job.backup        // что запущено: бэкап/архив или распаковка (pt_restore)
    private var lastArgs: [String] = []     // для «Запустить заново»
    @Published var queue: [Queued] = []
    @Published var batch: [Finished] = []   // итог показывается по всей пачке

    // вкладка «Разархивация»
    @Published var restoreSources: [URL] = []
    @AppStorage("restoreTo") var restoreTo = "/Volumes/Current projects/_temp"
    @AppStorage("restoreCopy") var restoreCopy = false
    @AppStorage("sourceDups") var sourceDups = false
    @AppStorage("onlySource") var onlySource = false
    @AppStorage("restoreWorkcopy") var restoreWorkcopy = false
    @AppStorage("restoreVideo") var restoreVideo = true
    var localIndex: Int { get { pipe.localIndex } set { pipe.localIndex = newValue } }  // «Сессия k/N» у распаковки
    @Published var dests: [String] = []
    @Published var dest: String = ""
    @Published var running = false
    @Published var finished = false
    @Published var exitCode: Int32 = 0
    // ход работы
    // конвейер pt_backup: сессия N+1 сжимается, пока сессия N уходит на сервер
    @Published var pipe = PipeState()
    let watch = ScheduleWatch()   // ночное задание идёт отдельно от окна — его ход читается из файла событий
    var local: Lane { get { pipe.local } set { pipe.local = newValue } }
    var upload: Lane { get { pipe.upload } set { pipe.upload = newValue } }
    var uploadIndex: Int { get { pipe.uploadIndex } set { pipe.uploadIndex = newValue } }
    var sessionTotal: Int { get { pipe.sessionTotal } set { pipe.sessionTotal = newValue } }
    var steps: [Step] { get { pipe.steps } set { pipe.steps = newValue } }
    @Published var log: [LogLine] = []
    var errors: Int { log.filter { $0.level == "error" }.count }

    @AppStorage("noWorkcopy") var noWorkcopy = true
    @AppStorage("lastDest") var lastDest = ""
    @Published var mode = Mode.backup     // какой кнопкой запущен текущий/последний прогон

    private var process: Process?
    private var input: Pipe?   // ответы движку на вопросы (удалять ли после архивации)

    init() {
        loadDests(); loadRuns(); loadSchedule()
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func loadDests() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [scriptPath, "--list-dests"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        try? p.run()
        p.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        dests = (try? JSONDecoder().decode([String].self, from: data)) ?? []
        if !lastDest.isEmpty && !dests.contains(lastDest) { dests.insert(lastDest, at: 0) }
        dest = lastDest.isEmpty ? (dests.first ?? "") : lastDest
    }

    /// Выбор папки назначения; новая попадает в список недавних. nil — отмена.
    func chooseDest(from start: String) -> String? {
        let p = NSOpenPanel()
        p.canChooseDirectories = true
        p.canChooseFiles = false
        p.canCreateDirectories = true
        p.prompt = "Бэкапить сюда"
        if FileManager.default.fileExists(atPath: start) { p.directoryURL = URL(fileURLWithPath: start) }
        guard p.runModal() == .OK, let u = p.url else { return nil }
        if !dests.contains(u.path) { dests.insert(u.path, at: 0) }
        return u.path
    }

    /// Проект распакован pt_restore — бэкапить туда же, откуда брали (запись .pt_restored.json).
    @Published var destHint = ""
    func pickDestFromRestore() {
        for u in sources {
            guard let d = try? Data(contentsOf: u.appendingPathComponent(".pt_restored.json")),
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let origin = j["origin"] as? String else { continue }
            let parent = (origin as NSString).deletingLastPathComponent
            if !dests.contains(parent) { dests.insert(parent, at: 0) }
            dest = parent
            destHint = "Куда — туда же, откуда \(u.lastPathComponent) распаковывали"
            return
        }
        destHint = ""
    }

    /// «Убрать из списка» — правит историю назначений pt_backup.
    func forgetDest(_ d: String) {
        dests.removeAll { $0 == d }
        let file = supportDir + "/destinations.json"
        let hist = ((try? JSONDecoder().decode([String].self, from: Data(contentsOf: URL(fileURLWithPath: file)))) ?? [])
            .filter { $0 != d }
        try? JSONEncoder().encode(hist).write(to: URL(fileURLWithPath: file))
        if lastDest == d { lastDest = "" }
        if dest == d { dest = dests.first ?? "" }
    }

    func options(_ mode: Mode) -> [String] {
        // без -y: после сверки с сервером движок спросит, удалять ли (см. case "ask")
        var a: [String] = mode == .archive ? ["--delete-source"] : []
        if noWorkcopy { a.append("--no-workcopy") }
        return a
    }

    /// Во время работы — в очередь, иначе сразу.
    func start(_ mode: Mode) {
        guard !sources.isEmpty, !dest.isEmpty else { return }
        enqueue(Queued(job: .backup, mode: mode, sources: sources, dest: dest,
                       args: [scriptPath, "--gui", "--dest", dest] + options(mode) + sources.map(\.path)))
        sources = []
    }

    private func enqueue(_ q: Queued) {
        if running { queue.append(q) } else { batch = []; run(q) }
    }

    private func run(_ q: Queued) {
        runSources = q.sources
        runDest = q.dest
        job = q.job
        mode = q.mode
        if q.job == .backup { lastDest = q.dest }
        launch(q.args)
    }

    /// Распаковка через pt_restore: с сетевого диска — копия в restoreTo, с локального — на месте.
    func startRestore() {
        guard !restoreSources.isEmpty else { return }
        var a = [restoreScript, "--gui", "--to", restoreTo]
        if restoreCopy { a.append("--copy") }
        if onlySource { a.append("--only-source") } else if sourceDups { a.append("--source-dups") }
        if restoreWorkcopy { a.append("--workcopy") }
        if restoreVideo && !onlySource { a.append("--video") }
        enqueue(Queued(job: .restore, mode: .backup, sources: restoreSources, dest: restoreTo,
                       args: a + restoreSources.map(\.path)))
        restoreSources = []
    }

    /// Повтор неудавшегося прогона с теми же проектами, папкой и опциями.
    /// Повтор одного задания из итога (во время работы — в очередь).
    func retry(_ f: Finished) {
        enqueue(Queued(job: f.job, mode: f.mode, sources: f.sources, dest: f.dest, args: f.args))
    }

    private func launch(_ args: [String]) {
        lastArgs = args
        running = true; finished = false
        pipe = PipeState()   // целиком: по полям забывались uploaded и phase прошлого задания («27 из 1»)
        log = []

        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = args
        p.environment = ProcessInfo.processInfo.environment.merging(
            ["PATH": enginePath, "PT_BACKUP_TRIGGER": "app"]) { $1 }
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardInput = inp
        input = inp
        p.standardOutput = out
        p.standardError = err
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.done(proc.terminationStatus) }
        }
        do { try p.run() } catch {
            log.append(LogLine(level: "error", text: "Не удалось запустить Python: \(error.localizedDescription)"))
            done(-1)   // в итог пачки как ошибка; очередь идёт дальше
            return
        }
        process = p
        read(out) { [weak self] line in self?.handle(line) }
        read(err) { [weak self] line in self?.log.append(LogLine(level: "error", text: line)) }
    }

    /// Заголовок хода работы — общий для окна и меню в верхнем баре.
    var progressTitle: String {
        if sessionTotal == 0 { return pipe.title }
        return job == .restore ? "Распаковано \(max(0, localIndex - 1)) из \(sessionTotal)"
 : pipe.title
    }

    func cancel() { process?.terminate() }  // SIGTERM: скрипт сам уберёт временные файлы

    private func done(_ code: Int32) {
        exitCode = code
        running = false
        finished = true
        process = nil
        input = nil
        loadDests()
        loadRuns()
        // runs.jsonl уже перечитан: первая запись — этот запуск (если движок успел её записать)
        let rec = runs.first.flatMap { $0.paths == runSources.map(\.path) ? $0 : nil }
        batch.append(Finished(job: job, mode: mode, sources: runSources, dest: runDest,
                              code: code, run: rec, args: lastArgs))
        if !queue.isEmpty && code != 130 {   // «Отменить» останавливает и очередь
            run(queue.removeFirst())
            return
        }
        if !NSApp.isActive { notify() }   // окно свёрнуто в меню — сообщаем итог уведомлением
    }

    private func notify() {
        let c = UNMutableNotificationContent()
        let bad = batch.filter { !$0.ok }.count
        c.title = bad == 0 ? "Готово: заданий \(batch.count)" : "С ошибками: \(bad) из \(batch.count)"
        c.body = batch.flatMap { $0.sources.map(\.lastPathComponent) }.joined(separator: ", ")
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    // MARK: история

    @Published var runs: [Run] = []

    /// Убрать записи из runs.jsonl (строки, которые не читаются, остаются как есть).
    /// Удалённые из истории записи — в отдельном файле: runs.jsonl пишет движок, окно его не переписывает
    /// (иначе запись запуска, закончившегося в этот момент, могла бы пропасть).
    private let hiddenRunsPath = supportDir + "/runs_hidden.json"
    private var hiddenRuns: Set<String> {
        guard let d = FileManager.default.contents(atPath: hiddenRunsPath) else { return [] }
        return Set((try? JSONDecoder().decode([String].self, from: d)) ?? [])
    }

    private func allRuns() -> [Run] {
        let text = (try? String(contentsOfFile: runsPath, encoding: .utf8)) ?? ""
        return text.split(separator: "\n").compactMap { try? JSONDecoder().decode(Run.self, from: Data($0.utf8)) }
    }

    func forgetRuns(where drop: (Run) -> Bool) {
        let hidden = hiddenRuns.union(allRuns().filter(drop).map(\.id))
        try? JSONEncoder().encode(Array(hidden)).write(to: URL(fileURLWithPath: hiddenRunsPath))
        loadRuns()
    }

    func loadRuns() {
        let hidden = hiddenRuns
        runs = allRuns().filter { !hidden.contains($0.id) }.suffix(300).reversed()
    }

    /// «Повторить»: вернуть проекты, назначение и режим запуска во вкладку «Бэкап».
    func repeatRun(_ r: Run) {
        if r.mode == "restore" {
            restoreSources = r.paths.map { URL(fileURLWithPath: $0, isDirectory: true) }
            restoreTo = r.dest
            return
        }
        sources = r.paths.map { URL(fileURLWithPath: $0, isDirectory: true) }
        dest = r.dest
        if !dests.contains(dest) { dests.insert(dest, at: 0) }
        finished = false; log = []
    }

    // MARK: расписание

    @Published var schedule: Schedule?
    @Published var scheduleError: String?   // загрузка задания в launchd идёт в фоне — её ошибка приходит сюда

    func loadSchedule() {
        guard FileManager.default.fileExists(atPath: agentPath),
              let d = FileManager.default.contents(atPath: schedulePath) else { schedule = nil; return }
        schedule = try? JSONDecoder().decode(Schedule.self, from: d)
    }

    /// Записывает schedule.json и LaunchAgent, перезагружает его в launchd. nil — удалить расписание.
    func saveSchedule(_ s: Schedule?) -> String? {
        let uid = "gui/\(getuid())"
        launchctl(["bootout", "\(uid)/\(agentLabel)"])   // старое задание, если было
        guard let s else {
            try? FileManager.default.removeItem(atPath: agentPath)
            try? FileManager.default.removeItem(atPath: schedulePath)
            schedule = nil
            return nil
        }
        // launchd: Weekday 0 = вс; все дни — один интервал без Weekday
        let times: [[String: Int]] = s.days.count == 7 ? [["Hour": s.hour, "Minute": s.minute]]
            : s.days.map { ["Hour": s.hour, "Minute": s.minute, "Weekday": $0 % 7] }
        let plist: [String: Any] = [
            "Label": agentLabel,
            // caffeinate -i: Mac не засыпает от бездействия, пока идёт бэкап (после пробуждения по pmset)
            "ProgramArguments": ["/usr/bin/caffeinate", "-i", pythonPath, scriptPath, "--daily", "--plain"]
                + (s.noVideo == true ? ["--no-video"] : s.noWorkcopy == false ? [] : ["--no-workcopy"])
                + (s.tags ?? []).flatMap { ["--tag", $0] }
                + ["--dest", s.dest] + s.paths,
            "EnvironmentVariables": ["PATH": enginePath, "PT_BACKUP_TRIGGER": "schedule",
                                     "PT_BACKUP_EVENTS": scheduleEventsPath],
            "StartCalendarInterval": times,
            "ProcessType": "Background",
        ]
        do {
            try FileManager.default.createDirectory(atPath: supportDir, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(atPath: (agentPath as NSString).deletingLastPathComponent,
                                                    withIntermediateDirectories: true)
            try JSONEncoder().encode(s).write(to: URL(fileURLWithPath: schedulePath))
            try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
                .write(to: URL(fileURLWithPath: agentPath))
        } catch { return error.localizedDescription }
        schedule = s
        scheduleError = nil
        // bootout завершается не сразу: пока старое задание не выгружено, bootstrap отвечает 5 —
        // повторяем в фоне, чтобы окно не замирало
        let path = agentPath
        Task { [weak self] in   // главный поток: результат — в окно
            let rc = await Task.detached { () -> Int32 in   // повторы — в фоне
                var rc = launchctl(["bootstrap", uid, path])
                for _ in 0..<20 where rc != 0 {
                    try? await Task.sleep(nanoseconds: 250_000_000)
                    rc = launchctl(["bootstrap", uid, path])
                }
                return rc
            }.value
            self?.scheduleError = rc == 0 ? nil : "launchctl bootstrap: код \(rc)"
        }
        return nil
    }

    func runScheduleNow() { launchctl(["kickstart", "gui/\(getuid())/\(agentLabel)"]) }


    /// Ближайший запуск по расписанию.
    var nextScheduled: Date? {
        guard let s = schedule else { return nil }
        let cal = Calendar.current
        return s.days.compactMap { d -> Date? in
            // Calendar: 1 = вс … 7 = сб
            cal.nextDate(after: Date(), matching: DateComponents(hour: s.hour, minute: s.minute, weekday: d % 7 + 1),
                         matchingPolicy: .nextTime)
        }.min()
    }

    /// Читает поток построчно (куски данных могут рвать строки посередине).
    private func read(_ pipe: Pipe, _ onLine: @escaping @MainActor (String) -> Void) {
        let h = pipe.fileHandleForReading
        Task.detached {
            var buf = Data()
            while true {
                let d = h.availableData
                if d.isEmpty { break }
                buf.append(d)
                while let nl = buf.firstIndex(of: 0x0A) {
                    let line = String(decoding: buf[buf.startIndex..<nl], as: UTF8.self)
                    buf.removeSubrange(buf.startIndex...nl)
                    if !line.isEmpty { await onLine(line) }
                }
            }
        }
    }

    private func handle(_ line: String) {
        guard let d = line.data(using: .utf8),
              let e = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let t = e["t"] as? String else {
            log.append(LogLine(level: "debug", text: line))
            return
        }
        switch t {
        case "header", "bar", "lane":
            _ = pipe.apply(e)
        case "ask":
            let question = e["text"] as? String ?? ""
            if !NSApp.isActive {   // окно свёрнуто — вернуть его, вопрос ждёт ответа
                NSApp.setActivationPolicy(.regular)
                NSApp.activate(ignoringOtherApps: true)
            }
            let alert = NSAlert()
            alert.messageText = "Архив на сервере сверен"
            alert.informativeText = question + "\n\nУдаляются только файлы, совпавшие с сервером; изменённые за время работы останутся."
            alert.alertStyle = .warning
            alert.addButton(withTitle: "Удалить").keyEquivalent = ""
            alert.addButton(withTitle: "Оставить").keyEquivalent = "\r"   // как [y/N] в терминале: по Return — не удалять
            let yes = alert.runModal() == .alertFirstButtonReturn
            input?.fileHandleForWriting.write(Data((yes ? "y\n" : "n\n").utf8))
        case "msg":
            let level = e["level"] as? String ?? "info"
            let text = e["text"] as? String ?? ""
            if level == "info" && text.contains(": не найдено") { return }  // «шаг: не найдено» — шум
            log.append(LogLine(level: level, text: text))
        default: break
        }
    }

    /// Общий прогресс: сессии до текущей + доля текущего шага внутри текущей.
    var overall: Double { pipe.overall(restore: job == .restore) }
}

// MARK: - Вьюхи

struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).padding(.leading, 4)
            VStack(alignment: .leading, spacing: 8) { content }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.08)))
        }
    }
}

struct Detail: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

/// Недавние папки назначения, сгруппированные по тому: «Storage A» → _amber, Steins Gate…
struct DestPicker: View {
    @ObservedObject var m: Model
    @Binding var selection: String

    var body: some View {
        let recent = Array(m.dests.prefix(3)) + (m.dests.prefix(3).contains(selection) || selection.isEmpty ? [] : [selection])
        let groups = Dictionary(grouping: recent, by: volume)
        let order = recent.map(volume).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        VStack(alignment: .leading, spacing: 10) {
            ForEach(order, id: \.self) { vol in
                VStack(alignment: .leading, spacing: 2) {
                    Label(vol, systemImage: vol == "Этот Mac" ? "desktopcomputer" : "externaldrive.fill")
                        .font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                        .padding(.leading, 8).padding(.bottom, 2)
                    ForEach(groups[vol] ?? [], id: \.self) { d in row(d) }
                }
            }
            Button {
                if let d = m.chooseDest(from: selection) { selection = d }
            } label: { Label("Другая папка…", systemImage: "folder.badge.plus").font(.system(size: 12)) }
                .buttonStyle(.borderless).padding(.leading, 8)
        }
    }

    func row(_ d: String) -> some View {
        let on = selection == d
        return Button { selection = d } label: {
            HStack(spacing: 8) {
                Image(systemName: on ? "checkmark.circle.fill" : "folder")
                    .foregroundStyle(Color.accentColor).frame(width: 16)
                // путь читается слева направо: «Sessions_2 › _amber › _2026», папка — выделена
                (Text(inside(d).isEmpty ? "" : inside(d) + " › ").foregroundColor(.secondary)
                 + Text((d as NSString).lastPathComponent).fontWeight(on ? .bold : .semibold))
                    .font(.system(size: 12)).lineLimit(1).truncationMode(.head)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8).frame(height: 26)
            .background(RoundedRectangle(cornerRadius: 6).fill(on ? Color.accentColor.opacity(0.18) : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(d)
        .contextMenu {
            Button("Показать в Finder") { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: d) }
            Button("Убрать из списка") { m.forgetDest(d) }
        }
    }

    /// Имя тома: /Volumes/Storage A/… → «Storage A», остальное — «Этот Mac».
    func volume(_ d: String) -> String {
        let c = (d as NSString).pathComponents
        return c.count > 2 && c[1] == "Volumes" ? c[2] : "Этот Mac"
    }

    /// Путь от тома до родительской папки: «Sessions_2 › _amber».
    func inside(_ d: String) -> String {
        var c = Array((d as NSString).pathComponents.dropFirst().dropLast())
        if c.first == "Volumes" { c.removeFirst(min(2, c.count)) }
        return c.joined(separator: " › ")
    }
}

/// Список папок проектов/сессий с перетаскиванием.
struct Sources: View {
    let title: String
    @Binding var urls: [URL]
    var locked = false
    @State private var targeted = false

    var body: some View {
        Card(title: title) {
            if urls.isEmpty {
                VStack(spacing: 6) {
                    Image(systemName: "folder.badge.plus").font(.system(size: 28)).foregroundStyle(.secondary)
                    Text("Перетащите сюда папку проекта или сессии").font(.system(size: 13))
                    Detail(text: "или нажмите, чтобы выбрать. Можно несколько, сессии внутри проекта найдутся сами.")
                }.frame(maxWidth: .infinity, minHeight: 110)
            } else {
                ForEach(urls, id: \.self) { u in
                    HStack {
                        Image(systemName: isSession(u) ? "waveform" : "folder")
                            .foregroundStyle(Color.accentColor).frame(width: 18)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(u.lastPathComponent).font(.system(size: 12, weight: .medium))
                            Detail(text: (isSession(u) ? "сессия · " : "проект · ") + u.deletingLastPathComponent().path)
                                .lineLimit(1).truncationMode(.head)
                        }
                        Spacer()
                        if !locked {
                            Button { urls.removeAll { $0 == u } } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if !urls.isEmpty {
                Label("Добавить ещё — нажмите или перетащите", systemImage: "plus")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {   // крестики у строк — свои кнопки, клик по ним сюда не доходит
            guard !locked else { return }
            let p = NSOpenPanel()
            p.canChooseDirectories = true; p.canChooseFiles = false; p.allowsMultipleSelection = true
            p.prompt = "Добавить"
            if p.runModal() == .OK { add(p.urls) }
        }
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.accentColor, lineWidth: targeted ? 2 : 0).padding(.top, 20))
        .onDrop(of: [.fileURL], isTargeted: $targeted) { providers in
            guard !locked else { return false }
            for pr in providers {
                pr.loadItem(forTypeIdentifier: UTType.fileURL.identifier) { item, _ in
                    if let d = item as? Data, let u = URL(dataRepresentation: d, relativeTo: nil) {
                        Task { @MainActor in add([u]) }
                    }
                }
            }
            return true
        }
    }

    func add(_ new: [URL]) {
        for u in new where (try? u.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
            if !urls.contains(u) { urls.append(u) }
        }
    }

    func isSession(_ u: URL) -> Bool {
        let items = (try? FileManager.default.contentsOfDirectory(atPath: u.path)) ?? []
        return items.contains { ["ptx", "ptf", "pts"].contains(($0 as NSString).pathExtension.lowercased()) }
    }
}

struct Progress: View {
    @ObservedObject var m: Model
    let kind: Job   // вкладка: бэкапы/архивы или распаковки — итог только своих
    @State private var shown: Run?
    var body: some View {
        Card(title: m.finished ? "Итог" : "Ход работы") {
            if m.finished {
                let batch = m.batch.filter { $0.job == kind }
                let queued = m.queue.filter { $0.job == kind }
                let bad = batch.filter { !$0.ok }
                let stopped = batch.contains { $0.code == 130 }
                HStack(spacing: 10) {
                    Image(systemName: bad.isEmpty ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 26))
                        .foregroundStyle(bad.isEmpty ? .green : stopped ? .orange : .red)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(bad.isEmpty ? (batch.count == 1 ? "Готово" : "Готово: все \(batch.count)")
                             : stopped ? "Остановлено" : "С ошибками: \(bad.count) из \(batch.count)")
                            .font(.system(size: 15, weight: .semibold))
                        if !queued.isEmpty { Detail(text: "Очередь не запускалась: \(queued.count)") }
                    }
                    Spacer()
                    if bad.count > 1 {
                        Button { bad.forEach(m.retry) } label: { Label("Повторить неудачные", systemImage: "arrow.clockwise") }
                    }
                }
                ForEach(batch) { f in
                    VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Image(systemName: f.ok ? "checkmark.circle.fill" : f.code == 130 ? "stop.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(f.ok ? .green : f.code == 130 ? .orange : .red)
                        Text((f.job == .restore ? "Распаковка" : f.mode == .archive ? "Архив" : "Бэкап") )
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.secondary.opacity(0.15)))
                        Text(f.sources.map(\.lastPathComponent).joined(separator: ", "))
                            .font(.system(size: 12, weight: .medium)).lineLimit(1)
                        Text("→ " + prettyPath(f.dest)).font(.system(size: 11)).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.head)
                        Spacer()
                        if let r = f.run, r.errors > 0 { Text("✗ \(r.errors)").font(.system(size: 10)).foregroundStyle(.red) }
                        if let r = f.run, r.warnings > 0 { Text("! \(r.warnings)").font(.system(size: 10)).foregroundStyle(.orange) }
                        if let r = f.run, r.log_offset != nil {
                            Button { shown = r } label: { Image(systemName: "doc.text.magnifyingglass") }
                                .buttonStyle(.borderless).help("Лог этого задания")
                        }
                        if !f.ok {
                            Button { m.retry(f) } label: { Image(systemName: "arrow.clockwise") }
                                .buttonStyle(.borderless).help("Запустить заново")
                        }
                    }
                    // что дал бэкап: новые файлы и «проект → на сервере»
                    if let st = f.run?.stats, f.job == .backup {
                        ForEach(st.lines, id: \.self) { line in
                            Text(line).font(.system(size: 11)).foregroundStyle(.secondary).padding(.leading, 22)
                        }
                    }
                    }
                }
                .sheet(item: $shown) { RunLogSheet(r: $0) }
            } else {
                HStack(alignment: .firstTextBaseline) {
                    Text(m.progressTitle).font(.system(size: 15, weight: .semibold))
                    Spacer()
                    Text("\(Int(m.overall * 100))%").font(.system(size: 12).monospacedDigit()).foregroundStyle(.secondary)
                }
                Detail(text: (m.job == .restore ? "Распаковка: " : m.mode == .archive ? "Архив: " : "Бэкап: ")
                       + m.runSources.map(\.lastPathComponent).joined(separator: ", ") + " → " + prettyPath(m.runDest))
                    .lineLimit(1).truncationMode(.middle)
                ProgressView(value: m.overall).tint(.accentColor)
            }
            // два столбца: что делается на этом Mac и что уходит на сервер; по окончании — только итог
            if !m.finished {
            HStack(alignment: .top, spacing: 12) {
                if m.job == .restore {
                    LaneColumn(title: "Распаковка", icon: "shippingbox", lane: m.local, tint: .green)
                } else {
                    LaneColumn(title: "Локально", icon: "cpu", lane: m.local, tint: .green)
                    LaneColumn(title: "На сервер", icon: "arrow.up.circle", lane: m.upload, tint: .accentColor,
                               localDone: m.local.finished)
                }
            }
            .padding(.top, 4)
            }
        }
    }
}

/// Столбец конвейера фиксированной высоты: что идёт, текущий шаг и его прогресс.
struct LaneColumn: View {
    let title: String, icon: String
    let lane: Lane
    let tint: Color
    var localDone = false   // для «На сервер»: локальная часть всё сжала

    /// Что писать вместо названия шага, когда шага сейчас нет.
    var idleText: String {
        if !lane.title.isEmpty { return lane.title }
        if lane.finished { return "всё сжато, идёт отправка" }
        if !lane.session.isEmpty { return "проверка перед отправкой" }
        if title == "На сервер" { return localDone ? "завершение: проверка остального" : "ждёт, пока сожмётся" }
        return "ждёт"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label(title, systemImage: icon).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint)
                Spacer()
                Text(lane.stage).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            }
            Text(lane.finished ? "Готово" : lane.session.isEmpty ? "—" : lane.session)
                .font(.system(size: 14, weight: .semibold)).lineLimit(1).truncationMode(.middle)
            HStack {
                Text(idleText)
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Text(lane.title.isEmpty ? "" : "\(Int(lane.fraction * 100))%")
                    .font(.system(size: 12, weight: .medium).monospacedDigit())
            }
            ProgressView(value: lane.fraction).tint(tint)
            Text(lane.detail.isEmpty ? " " : lane.detail)
                .font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(tint.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(tint.opacity(0.25)))
    }
}

/// Вкладка «Разархивация»: pt_restore — FLAC → WAV, бэкапы сессий, дубликаты, видео.
struct RestoreView: View {
    @ObservedObject var m: Model
    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            ScrollView {
                VStack(spacing: 14) {
                    Card(title: "Куда копировать") {
                        HStack(spacing: 8) {
                            Image(systemName: "folder").foregroundStyle(Color.accentColor)
                            VStack(alignment: .leading, spacing: 1) {
                                Text((m.restoreTo as NSString).lastPathComponent).font(.system(size: 12, weight: .semibold))
                                Detail(text: prettyPath(m.restoreTo)).lineLimit(1).truncationMode(.head)
                            }
                            Spacer(minLength: 0)
                        }
                        .help(m.restoreTo)
                        Button {
                            let p = NSOpenPanel()
                            p.canChooseDirectories = true; p.canChooseFiles = false; p.canCreateDirectories = true
                            p.prompt = "Распаковывать сюда"
                            p.directoryURL = URL(fileURLWithPath: m.restoreTo)
                            if p.runModal() == .OK, let u = p.url { m.restoreTo = u.path }
                        } label: { Label("Другая папка…", systemImage: "folder.badge.plus").font(.system(size: 12)) }
                            .buttonStyle(.borderless)
                        Detail(text: "Папки с сервера копируются сюда и распаковываются в копии, сервер не меняется. "
                               + "Папки с локального диска распаковываются на месте.")
                    }
                    Card(title: "Что восстановить") {
                        Toggle("Копировать и с локального диска", isOn: $m.restoreCopy)
                            .help("Не распаковывать на месте, а сначала скопировать в папку слева (--copy)")
                        Toggle("Дубликаты в Source — файлами", isOn: $m.sourceDups)
                            .help("Иначе в Source остаются ссылки .ptref на файлы сессий (--source-dups)")
                            .disabled(m.onlySource)
                        Toggle("Только Source, без сессий", isOn: $m.onlySource)
                            .help("--only-source")
                        Toggle("Видео сессий — скопировать с сервера", isOn: $m.restoreVideo)
                            .help("Если распакованы отдельные сессии, а их видео лежит в папке проекта или вне проекта — "
                                  + "скопировать его в «Video Files» сессии (--video)")
                            .disabled(m.onlySource)
                        Toggle("Пересобрать рабочие копии видео", isOn: $m.restoreWorkcopy)
                            .help("*_Workcopy_*.mov через mix_prep — долго (--workcopy)")
                    }
                }
                .padding(14)
            }
            .frame(width: 280)
            Divider()
            ScrollView {
                VStack(spacing: 14) {
                    Sources(title: m.running ? "Добавить в очередь" : "Что распаковать", urls: $m.restoreSources)
                    HStack {
                        Spacer()
                        if m.running {
                            Button("Отменить", role: .cancel) { m.cancel() }.controlSize(.large)
                                .help("Остановить текущее; очередь не запустится")
                        }
                        Button(m.running ? "Распаковку в очередь" : "Распаковать") { m.startRestore() }
                            .buttonStyle(.borderedProminent).controlSize(.large)
                            .keyboardShortcut(.defaultAction)
                            .disabled(m.restoreSources.isEmpty)
                    }
                    if m.running ? m.job == .restore : m.batch.contains(where: { $0.job == .restore }) {
                        Progress(m: m, kind: .restore)
                    }
                    QueueView(m: m, kind: .restore)
                }
                .padding(14)
            }
        }
    }
}

/// Очередь заданий (общая для бэкапа и распаковки).
struct QueueView: View {
    @ObservedObject var m: Model
    let kind: Job
    var body: some View {
        let queued = m.queue.filter { $0.job == kind }
        if !queued.isEmpty {
            Card(title: "В очереди: \(queued.count)") {
                ForEach(queued) { q in
                    HStack(spacing: 8) {
                        Image(systemName: "clock").foregroundStyle(.secondary)
                        Text(q.job == .restore ? "Распаковка" : q.mode == .archive ? "Архив" : "Бэкап")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.secondary.opacity(0.15)))
                        Text(q.sources.map(\.lastPathComponent).joined(separator: ", "))
                            .font(.system(size: 12, weight: .medium)).lineLimit(1)
                        Text("→ " + prettyPath(q.dest)).font(.system(size: 11)).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.head)
                        Spacer()
                        Button { m.queue.removeAll { $0.id == q.id } } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary).help("Убрать из очереди")
                    }
                }
            }
        }
    }
}

struct LaneRow: View {
    let label: String
    let lane: Lane
    let tint: Color
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(label).font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 64, alignment: .leading)
                Text(lane.session.isEmpty ? "—" : lane.session).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                Text(lane.title.isEmpty ? (lane.session.isEmpty ? "" : "ждёт") : lane.title)
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Text(lane.detail).font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
            }
            ProgressView(value: lane.fraction).tint(tint)
        }
    }
}

enum Tab: String, CaseIterable, Identifiable {
    case backup = "Бэкап", restore = "Разархивация", history = "История", schedule = "Расписание"
    var id: String { rawValue }
}

struct ContentView: View {
    @ObservedObject var m: Model
    @AppStorage("tab") private var tab = Tab.backup.rawValue
    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                ForEach(Tab.allCases) { Text($0.rawValue).tag($0.rawValue) }
            }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 480).padding(10)
            Divider()
            switch Tab(rawValue: tab) ?? .backup {
            case .backup:
                BackupView(m: m)
            case .history:
                VStack(spacing: 14) { HistoryView(m: m) { tab = $0.rawValue } }.padding(14)
            case .restore:
                RestoreView(m: m)
            case .schedule:
                ScheduleView(m: m)
            }
        }
        .frame(minWidth: 800, minHeight: 600, maxHeight: .infinity, alignment: .top)
    }
}

struct BackupView: View {
    @ObservedObject var m: Model
    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            ScrollView {
                VStack(spacing: 14) {
                    Card(title: "Куда бэкапить") { DestPicker(m: m, selection: $m.dest) }
                    Card(title: "Опции") {
                        Toggle("Не отправлять рабочие копии видео", isOn: $m.noWorkcopy)
                    }
                }
                .padding(14)
            }
            .frame(width: 280)
            Divider()
            ScrollView {
                VStack(spacing: 14) {
                    Sources(title: m.running ? "Добавить в очередь" : "Что бэкапить", urls: $m.sources)
                        .onChange(of: m.sources) { m.pickDestFromRestore() }
                    HStack {
                        if m.dest.isEmpty { Detail(text: "Выберите слева, куда бэкапить") }
                        else if !m.destHint.isEmpty && !m.sources.isEmpty { Detail(text: m.destHint) }
                        Spacer()
                        if m.running {
                            Button("Отменить", role: .cancel) { m.cancel() }.controlSize(.large)
                                .help("Остановить текущее; очередь не запустится")
                        }
                        Group {
                            Button(m.running ? "Архив в очередь" : "Архивировать") { m.start(.archive) }
                                .controlSize(.large)
                                .help("Бэкап, сверка с сервером, затем вопрос — удалить ли локальную папку")
                            Button(m.running ? "Бэкап в очередь" : "Бэкапить") { m.start(.backup) }
                                .buttonStyle(.borderedProminent).controlSize(.large)
                                .keyboardShortcut(.defaultAction)
                        }.disabled(m.sources.isEmpty || m.dest.isEmpty)
                    }
                    if m.running ? m.job == .backup : m.batch.contains(where: { $0.job == .backup }) {
                        Progress(m: m, kind: .backup)
                    }
                    QueueView(m: m, kind: .backup)
                }
                .padding(14)
            }
        }
    }
}

func modeName(_ mode: String) -> String {
    ["daily": "Daily", "archive": "Архив", "restore": "Распаковка"][mode] ?? "Бэкап"
}

struct HistoryView: View {
    @ObservedObject var m: Model
    let onRepeat: (Tab) -> Void   // открыть вкладку, куда вернули проекты
    @State private var shown: Run?
    @State private var confirmClear = false
    var body: some View {
        Card(title: "История запусков") {
            if m.runs.isEmpty {
                Detail(text: "Пока пусто. Сюда попадают все запуски pt_backup: из окна, сервисов Finder и по расписанию.")
                    .frame(maxWidth: .infinity, minHeight: 80)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(m.runs) { r in
                            RunRow(r: r, onRepeat: r.mode == "daily" || m.running ? nil : {
                                       m.repeatRun(r); onRepeat(r.mode == "restore" ? .restore : .backup)
                                   }, onLog: r.log_offset == nil ? nil : { shown = r })
                                .contextMenu { Button("Удалить из истории") { m.forgetRuns { $0.id == r.id } } }
                            Divider()
                        }
                    }
                }
            }
        }
        HStack {
            Button("Обновить") { m.loadRuns() }
            Button("Очистить историю…") { confirmClear = true }.disabled(m.runs.isEmpty)
            Spacer()
            Button("Лог бэкапов") { openLog() }
            Button("Лог распаковки") { openLog(restoreLogPath) }
        }
        .confirmationDialog("Очистить историю запусков?", isPresented: $confirmClear) {
            Button("Очистить", role: .destructive) { m.forgetRuns { _ in true } }
        } message: {
            Text("Список запусков удалится. Логи и бэкапы на сервере не трогаются.")
        }
        .onAppear { m.loadRuns() }
        .sheet(item: $shown) { RunLogSheet(r: $0) }
    }
}

/// Лог одного запуска; ошибки красным, предупреждения оранжевым.
struct RunLogSheet: View {
    let r: Run
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        let text = r.logText()
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(r.paths.map { ($0 as NSString).lastPathComponent }.joined(separator: ", "))
                        .font(.system(size: 14, weight: .semibold))
                    Detail(text: (r.date?.formatted(date: .abbreviated, time: .shortened) ?? r.start) + " → " + prettyPath(r.dest))
                }
                Spacer()
                Button("Весь лог в Console") { openLog() }
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(Array((text ?? "Этот кусок лога уже не найден (лог ротирован дважды).")
                        .split(separator: "\n").enumerated()), id: \.offset) { _, l in
                        Text(String(l))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(l.contains("✗") || l.contains("Traceback") ? Color.red
                                             : l.contains("] ! ") ? Color.orange : Color.primary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.textSelection(.enabled)
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
        }
        .padding(16)
        .frame(width: 860, height: 560)
    }
}

struct RunRow: View {
    let r: Run
    var onRepeat: (() -> Void)?
    var onLog: (() -> Void)?

    /// «→ куда · что дал бэкап» — отдельно: внутри body такое выражение Swift не успевает вывести
    var destLine: String {
        var s = "→ " + prettyPath(r.dest)
        if let st = r.stats {
            s += " · " + st.lines.joined(separator: " | ")
        }
        return s
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
        HStack(spacing: 8) {
            Image(systemName: r.rc == 0 ? "checkmark.circle.fill" : r.rc == 130 ? "stop.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(r.rc == 0 ? .green : r.rc == 130 ? .orange : .red)
                .font(.system(size: 12))
            Text(r.paths.map { ($0 as NSString).lastPathComponent }.joined(separator: ", "))
                .font(.system(size: 12, weight: .medium)).lineLimit(1)
            Text(modeName(r.mode) + (r.dry ? " · пробный" : "") + (r.trigger == "schedule" ? " · ⏰" : ""))
                .font(.system(size: 10)).foregroundStyle(.secondary)
                .padding(.horizontal, 5).padding(.vertical, 1)
                .background(Capsule().fill(Color.secondary.opacity(0.15)))
            if r.errors > 0 { Text("✗ \(r.errors)").font(.system(size: 10)).foregroundStyle(.red) }
            if r.warnings > 0 { Text("! \(r.warnings)").font(.system(size: 10)).foregroundStyle(.orange) }
            Spacer(minLength: 8)
            Text((r.date.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? r.start) + " · "
                 + Duration.seconds(r.duration).formatted(.units(allowed: [.hours, .minutes, .seconds], width: .narrow)))
                .font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
            if let onLog {
                Button(action: onLog) { Image(systemName: "doc.text.magnifyingglass") }
                    .buttonStyle(.borderless).help("Лог этого запуска")
            }
            if let onRepeat {
                Button(action: onRepeat) { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).help("Повторить: вернуть проекты и папку во вкладку «Бэкап»")
            }
        }
        Text(destLine)
            .font(.system(size: 11)).foregroundStyle(.secondary)
            .lineLimit(1).truncationMode(.head).padding(.leading, 20)
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { onLog?() }
        .help(r.paths.joined(separator: "\n") + "\n→ " + r.dest)
    }
}

/// Daily по расписанию: свои проекты и назначение, независимо от вкладки «Бэкап».
struct ScheduleView: View {
    @ObservedObject var m: Model
    @State private var paths: [URL] = []
    @State private var dest = ""
    @State private var time = Calendar.current.date(from: DateComponents(hour: 3, minute: 0)) ?? Date()
    @State private var tags = Set<String>()
    @State private var noWorkcopy = true
    @State private var noVideo = false
    @State private var showTags = false
    @State private var error: String?

    var draft: Schedule {
        let c = Calendar.current.dateComponents([.hour, .minute], from: time)
        let old = m.schedule?.paths ?? []
        return Schedule(hour: c.hour ?? 3, minute: c.minute ?? 0,
                        paths: old + paths.map(\.path).filter { !old.contains($0) }, dest: dest,
                        tags: tags.isEmpty ? nil : tags.sorted(), noWorkcopy: noWorkcopy ? nil : false,
                        noVideo: noVideo ? true : nil)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            ScrollView {
                VStack(spacing: 14) {
                    Card(title: "Куда бэкапить") {
                        HStack(spacing: 8) {
                            Image(systemName: "externaldrive.fill").foregroundStyle(Color.accentColor)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(dest.isEmpty ? "Папка не выбрана" : (dest as NSString).lastPathComponent)
                                    .font(.system(size: 12, weight: .semibold))
                                if !dest.isEmpty { Detail(text: prettyPath(dest)).lineLimit(1).truncationMode(.head) }
                            }
                            Spacer(minLength: 0)
                        }
                        .help(dest)
                        Button { if let d = m.chooseDest(from: dest) { dest = d } } label: {
                            Label("Другая папка…", systemImage: "folder.badge.plus").font(.system(size: 12))
                        }.buttonStyle(.borderless)
                    }
                    Card(title: "Когда") {
                        DatePicker("Каждый день в", selection: $time, displayedComponents: .hourAndMinute).fixedSize()
                        Detail(text: "Режим Daily: проекты не меняются, сжимается временная копия, на сервер уходит только новое. "
                               + "Если Mac спал, задание выполнится после пробуждения.")
                    }
                    Card(title: "Опции") {
                        Toggle("Не отправлять видео совсем", isOn: $noVideo)
                            .help("mov, mp4 и т.п. — ни из проекта, ни лежащее вне проекта")
                        Toggle("Не отправлять рабочие копии видео (mov)", isOn: $noWorkcopy)
                            .help("Рабочая копия mix_prep с исходником рядом уходит исходником + записью в Workcopies.ptwork")
                            .disabled(noVideo)
                    }
                }
                .padding(14)
            }
            .frame(width: 280)
            Divider()
            ScrollView {
                VStack(spacing: 14) {
                    ScheduleProgress(w: m.watch)
                    if let s = m.schedule { current(s) }
                    Sources(title: m.schedule == nil ? "Проекты или папка с проектами" : "Добавить в расписание",
                            urls: $paths)
                    HStack(spacing: 8) {
                        Text("Только папки с тегами:").font(.system(size: 12))
                        // список с галочками во всплывающей панели: Menu на macOS закрывается после каждого пункта
                        Button { showTags.toggle() } label: {
                            HStack(spacing: 4) {
                                Image(systemName: "tag")
                                Text(tags.isEmpty ? "любые — без отбора" : tags.sorted().joined(separator: ", "))
                                Image(systemName: "chevron.down").font(.system(size: 9))
                            }
                        }
                        .popover(isPresented: $showTags, arrowEdge: .bottom) {
                            VStack(alignment: .leading, spacing: 6) {
                                ForEach(availableTags(draft.paths), id: \.self) { t in
                                    Toggle(t, isOn: Binding(get: { tags.contains(t) },
                                                            set: { if $0 { tags.insert(t) } else { tags.remove(t) } }))
                                }
                                if !tags.isEmpty {
                                    Divider()
                                    Button("Снять все") { tags = [] }.buttonStyle(.borderless)
                                }
                            }
                            .padding(12)
                            .frame(minWidth: 200, alignment: .leading)
                        }
                        Spacer()
                    }
                    .help("С тегом из папки в расписании бэкапятся только папки внутри неё с этим тегом (проверяется при каждом запуске)")
                    if !tags.isEmpty && !paths.isEmpty {
                        Card(title: "Будут бэкапиться по тегам") { matched(paths.map(\.path)) }
                    }
                    HStack {
                        if let e = error ?? m.scheduleError { Text(e).font(.system(size: 11)).foregroundStyle(.red) }
                        Spacer()
                        if m.schedule != nil && m.schedule == draft {
                            Label("Сохранено", systemImage: "checkmark").foregroundStyle(.secondary)
                        } else {
                            Button(m.schedule == nil ? "Включить расписание" : paths.isEmpty ? "Сохранить изменения" : "Добавить в расписание") {
                                let d = (dest as NSString).standardizingPath
                                if let p = draft.paths.first(where: { p in
                                    (d as NSString).appendingPathComponent((p as NSString).lastPathComponent) == p
                                        || d == p || d.hasPrefix(p + "/") }) {
                                    error = "\((p as NSString).lastPathComponent) уже лежит в папке назначения — бэкап был бы сам в себя"
                                    return
                                }
                                error = m.saveSchedule(draft)
                                if error == nil { paths = [] }   // добавленное уже в «Сейчас в расписании»
                            }
                            .buttonStyle(.borderedProminent).controlSize(.large)
                            .disabled(draft.paths.isEmpty || dest.isEmpty)
                        }
                    }
                }
                .padding(14)
            }
        }
        .onAppear {
            m.loadSchedule(); m.loadRuns()
            if let s = m.schedule {
                dest = s.dest
                time = Calendar.current.date(from: DateComponents(hour: s.hour, minute: s.minute)) ?? time
                tags = Set(s.tags ?? [])
                noWorkcopy = s.noWorkcopy ?? true
                noVideo = s.noVideo ?? false
            } else if dest.isEmpty {
                dest = m.dest
            }
        }
    }

    /// Какие проекты подходят под теги (на сейчас; при запуске проверяется заново).
    func matched(_ ps: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(ps, id: \.self) { p in
                let hits = taggedFolders(p, tags: tags)
                Text((p as NSString).lastPathComponent).font(.system(size: 12, weight: .semibold))
                if hits.isEmpty {
                    Detail(text: "нет папок с тегами — ничего не бэкапится").padding(.leading, 14)
                } else {
                    ForEach(hits, id: \.self) { h in
                        Label((h as NSString).lastPathComponent, systemImage: "checkmark.circle.fill")
                            .font(.system(size: 12)).foregroundStyle(.green).padding(.leading, 14)
                    }
                }
            }
        }
    }

    /// Что уже стоит в расписании.
    func current(_ s: Schedule) -> some View {
        Card(title: "Сейчас в расписании") {
            HStack(spacing: 10) {
                Image(systemName: "calendar.badge.clock").font(.system(size: 22)).foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 2) {
                    Text(String(format: "каждый день в %02d:%02d", s.hour, s.minute))
                        .font(.system(size: 14, weight: .semibold))
                    if let n = m.nextScheduled {
                        Detail(text: "Следующий запуск: " + n.formatted(date: .abbreviated, time: .shortened))
                    }
                }
                Spacer()
                Button("Запустить сейчас") { m.runScheduleNow() }
            }
            if let t = s.tags, !t.isEmpty {
                Label("только папки с тегами: " + t.joined(separator: ", "), systemImage: "tag")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            ForEach(s.paths, id: \.self) { p in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        Label((p as NSString).lastPathComponent, systemImage: "folder").font(.system(size: 12)).help(p)
                        if let t = s.tags, !t.isEmpty {
                            let hits = taggedFolders(p, tags: Set(t))
                            Detail(text: hits.isEmpty ? "сейчас под теги ничего не подходит"
                                   : "→ " + hits.map { ($0 as NSString).lastPathComponent }.joined(separator: ", "))
                                .padding(.leading, 20)
                        }
                    }
                    Spacer()
                    Button {
                        var n = s
                        n.paths.removeAll { $0 == p }
                        error = m.saveSchedule(n.paths.isEmpty ? nil : n)
                    } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain).foregroundStyle(.secondary).help("Убрать из расписания")
                }
            }
            Label(prettyPath(s.dest) + (s.noVideo == true ? " · без видео" : s.noWorkcopy == false ? "" : " · без рабочих копий mov"),
                  systemImage: "arrow.right.circle").font(.system(size: 12)).foregroundStyle(.secondary)
            if let last = m.runs.first(where: { $0.trigger == "schedule" }) {
                Divider()
                RunRow(r: last)
            }
            HStack {
                Spacer()
                Button("Удалить расписание", role: .destructive) { _ = m.saveSchedule(nil); paths = [] }
                    .controlSize(.small)
            }
        }
    }
}

/// Ход ночного задания (виден, пока оно идёт).
struct ScheduleProgress: View {
    @ObservedObject var w: ScheduleWatch
    var body: some View {
        if w.running {
            Card(title: "Идёт сейчас") {
                HStack(alignment: .firstTextBaseline) {
                    Text(w.pipe.title).font(.system(size: 15, weight: .semibold))
                    Spacer()
                    Text("\(Int(w.pipe.overall(restore: false) * 100))%")
                        .font(.system(size: 12).monospacedDigit()).foregroundStyle(.secondary)
                }
                ProgressView(value: w.pipe.overall(restore: false)).tint(.accentColor)
                HStack(alignment: .top, spacing: 12) {
                    LaneColumn(title: "Локально", icon: "cpu", lane: w.pipe.local, tint: .green)
                    LaneColumn(title: "На сервер", icon: "arrow.up.circle", lane: w.pipe.upload, tint: .accentColor,
                               localDone: w.pipe.local.finished)
                }
            }
        }
    }
}

/// Иконка в верхнем баре: ход текущего бэкапа, расписание, открыть окно.
struct MenuBarView: View {
    @ObservedObject var m: Model
    let updater: Updater
    @Environment(\.openWindow) private var openWindow
    private func showWindow() {
        NSApp.setActivationPolicy(.regular)
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if m.running {
                Text(m.progressTitle).font(.system(size: 13, weight: .semibold))
                ProgressView(value: m.overall)
                LaneRow(label: m.job == .restore ? "Распаковка" : "Локально", lane: m.local, tint: .green)
                if m.job == .backup { LaneRow(label: "На сервер", lane: m.upload, tint: .accentColor) }
                Button("Отменить") { m.cancel() }
            } else if let r = m.runs.first {
                Text("Последний: " + r.paths.map { ($0 as NSString).lastPathComponent }.joined(separator: ", "))
                    .font(.system(size: 12, weight: .medium)).lineLimit(1)
                Detail(text: (r.rc == 0 ? "успешно" : r.rc == 130 ? "остановлен" : "с ошибками") + " · "
                       + (r.date?.formatted(date: .abbreviated, time: .shortened) ?? r.start))
            } else {
                Detail(text: "Запусков ещё не было")
            }
            if let n = m.nextScheduled {
                Detail(text: "Daily по расписанию: " + n.formatted(date: .abbreviated, time: .shortened))
            }
            Divider()
            Button("Открыть окно") { showWindow() }
            Button("Проверить обновления…") { showWindow(); updater.check(manual: true) }  // ответ — в окне
            Button("Выйти") { m.cancel(); NSApp.terminate(nil) }
        }
        .padding(12)
        .frame(width: 280, alignment: .leading)
        .onAppear { m.loadRuns(); m.loadSchedule() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    // закрытое окно = свернуть в иконку в верхнем баре; бэкап продолжается
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationDidFinishLaunching(_ notification: Notification) {
        installEngine()
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { n in
            guard (n.object as? NSWindow)?.identifier?.rawValue.hasPrefix("main") == true else { return }
            NSApp.setActivationPolicy(.accessory)   // без иконки в Dock
        }
    }
}

/// Движок лежит в программе (Resources/engine) и после установки новой версии копируется в /usr/local/bin:
/// оттуда его запускают окно, ночное задание и сервисы Finder. Только при смене версии — правки движка
/// на месте при разработке не затираются.
func installEngine() {
    let key = "engineInstalledFor", fm = FileManager.default
    guard let src = Bundle.main.resourceURL?.appendingPathComponent("engine"),
          let names = try? fm.contentsOfDirectory(atPath: src.path), !names.isEmpty else { return }
    let missing = [scriptPath, restoreScript].contains { !fm.fileExists(atPath: $0) }
    guard missing || UserDefaults.standard.string(forKey: key) != appVersion else { return }
    do {
        for n in names where n.hasSuffix(".py") {
            let dst = "/usr/local/bin/" + n, tmp = dst + ".new"
            try? fm.removeItem(atPath: tmp)
            try fm.copyItem(atPath: src.appendingPathComponent(n).path, toPath: tmp)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: tmp)
            _ = try fm.replaceItemAt(URL(fileURLWithPath: dst), withItemAt: URL(fileURLWithPath: tmp))
        }
        UserDefaults.standard.set(appVersion, forKey: key)
    } catch {
        let a = NSAlert()
        a.messageText = "Не удалось установить движок в /usr/local/bin"
        a.informativeText = "\(error.localizedDescription)\n\nВ Терминале:\nsudo mkdir -p /usr/local/bin && sudo chown $USER /usr/local/bin\nи перезапустите PT Backup."
        a.runModal()
    }
}

@main
struct PTBackupApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var m = Model()
    @StateObject private var updater = Updater()
    var body: some Scene {
        Window("PT Backup", id: "main") {
            ContentView(m: m).modifier(UpdateUI(u: updater))
                .onAppear { updater.canInstall = { [weak m] in !(m?.running ?? false) } }
        }
            .windowResizability(.contentMinSize)
            .commands {
                CommandGroup(after: .appInfo) { Button("Проверить обновления…") { updater.check(manual: true) } }
            }
        MenuBarExtra {
            MenuBarView(m: m, updater: updater)
        } label: {
            if m.running {
                Image(systemName: "arrow.triangle.2.circlepath")
                Text("\(Int(m.overall * 100))%")
            } else {
                Image(systemName: "externaldrive.badge.timemachine")
            }
        }
        .menuBarExtraStyle(.window)
    }
}
