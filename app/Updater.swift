// Автообновление из GitHub Releases: у последнего релиза вложены latest.json и zip с приложением.
// Манифест подписан Ed25519 (закрытый ключ только у автора), архив проверяется по SHA-256.
// Установка всегда только после подтверждения пользователя.
import SwiftUI
import AppKit
import CryptoKit

let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"

struct UpdateManifest: Decodable {
    let version: String
    let file: String
    let sha256: String
    let size: Int
    let notes: String?
    let signature: String
}

enum UpdateError: LocalizedError {
    case msg(String)
    var errorDescription: String? { if case .msg(let s) = self { return s }; return nil }
}

func versionIsNewer(_ a: String, than b: String) -> Bool {
    let x = a.split(separator: ".").map { Int($0) ?? 0 }
    let y = b.split(separator: ".").map { Int($0) ?? 0 }
    for i in 0..<max(x.count, y.count) {
        let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
        if p != q { return p > q }
    }
    return false
}

@MainActor
final class Updater: ObservableObject {
    enum Phase: Equatable { case idle, checking, available, downloading, installing, upToDate, failed(String) }
    @Published var phase: Phase = .idle
    @Published var manifest: UpdateManifest?
    @Published var askInstall = false
    @Published var showResult = false      // показать итог ручной проверки (актуально / ошибка)
    /// false, пока программа занята (идёт бэкап): перезапуск ради обновления оборвал бы работу
    var canInstall: () -> Bool = { true }

    /// Приложение запущено из карантинной папки (Загрузки): macOS исполняет его из временной копии только для чтения.
    static var isTranslocated: Bool { Bundle.main.bundlePath.contains("/AppTranslocation/") }

    /// Вложение последнего релиза (GitHub сам перенаправляет на файл нужной версии).
    private func assetURL(_ name: String) -> URL {
        URL(string: "https://github.com/\(UpdateConfig.repo)/releases/latest/download/")!.appendingPathComponent(name)
    }

    private func fetch(_ url: URL) async throws -> Data {
        let (d, r) = try await URLSession.shared.data(from: url)
        guard (r as? HTTPURLResponse)?.statusCode == 200 else {
            throw UpdateError.msg("Сервер обновлений ответил \((r as? HTTPURLResponse)?.statusCode ?? 0)")
        }
        return d
    }

    func check(manual: Bool) {
        if phase == .checking || phase == .downloading || phase == .installing { return }
        phase = .checking
        Task {
            do {
                let m = try JSONDecoder().decode(UpdateManifest.self, from: try await fetch(assetURL("latest.json")))
                guard verify(m) else { throw UpdateError.msg("Подпись обновления неверна") }
                if versionIsNewer(m.version, than: appVersion) {
                    manifest = m; phase = .available; askInstall = true
                } else {
                    phase = .upToDate
                    if manual { showResult = true }
                }
            } catch {
                phase = .failed(error.localizedDescription)
                if manual { showResult = true }
            }
        }
    }

    private func verify(_ m: UpdateManifest) -> Bool {
        guard let pk = Data(base64Encoded: UpdateConfig.publicKey),
              let key = try? Curve25519.Signing.PublicKey(rawRepresentation: pk),
              let sig = Data(base64Encoded: m.signature) else { return false }
        return key.isValidSignature(sig, for: Data("\(m.version)\n\(m.file)\n\(m.sha256)\n\(m.size)".utf8))
    }

    func install() {
        guard let m = manifest else { return }
        guard canInstall() else {
            phase = .failed("Сейчас идёт работа — перезапуск её оборвёт. Установите обновление после окончания: «Проверить обновления…».")
            showResult = true
            return
        }
        phase = .downloading
        Task {
            do {
                let fm = FileManager.default
                let work = fm.temporaryDirectory.appendingPathComponent("update-\(UUID().uuidString)")
                try fm.createDirectory(at: work, withIntermediateDirectories: true)
                let (tmp, r) = try await URLSession.shared.download(from: assetURL(m.file))
                guard (r as? HTTPURLResponse)?.statusCode == 200 else { throw UpdateError.msg("Не удалось скачать \(m.file)") }
                let zip = work.appendingPathComponent("update.zip")
                try fm.moveItem(at: tmp, to: zip)
                let data = try Data(contentsOf: zip, options: .mappedIfSafe)
                guard data.count == m.size,
                      SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == m.sha256
                else { throw UpdateError.msg("Скачанный файл повреждён (контрольная сумма не совпала)") }
                phase = .installing
                let unz = work.appendingPathComponent("new")
                try run("/usr/bin/ditto", ["-x", "-k", zip.path, unz.path])
                let newApp = unz.appendingPathComponent(Bundle.main.bundleURL.lastPathComponent)
                guard let info = NSDictionary(contentsOf: newApp.appendingPathComponent("Contents/Info.plist")),
                      info["CFBundleIdentifier"] as? String == Bundle.main.bundleIdentifier,
                      info["CFBundleShortVersionString"] as? String == m.version
                else { throw UpdateError.msg("Содержимое архива не совпадает с описанием обновления") }
                var dest = Bundle.main.bundleURL
                if Updater.isTranslocated {   // запуск из Загрузок: macOS даёт копию только для чтения — ставим в «Программы»
                    let name = dest.lastPathComponent
                    let apps = URL(fileURLWithPath: "/Applications")
                    let home = fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
                    if fm.isWritableFile(atPath: apps.path) {
                        dest = apps.appendingPathComponent(name)
                    } else {
                        try? fm.createDirectory(at: home, withIntermediateDirectories: true)
                        dest = home.appendingPathComponent(name)
                    }
                }
                guard fm.isWritableFile(atPath: dest.deletingLastPathComponent().path) else {
                    throw UpdateError.msg("Нет прав заменить программу в \(dest.deletingLastPathComponent().path). Перенесите её в «Программы» и повторите.")
                }
                let script = work.appendingPathComponent("swap.sh")
                try """
                #!/bin/sh
                while kill -0 \(ProcessInfo.processInfo.processIdentifier) 2>/dev/null; do sleep 0.3; done
                DEST="\(dest.path)"
                NEW="\(newApp.path)"
                BAK="\(work.path)/old.app"
                if [ -e "$DEST" ]; then mv "$DEST" "$BAK" || exit 1; fi
                if ditto "$NEW" "$DEST"; then
                    xattr -cr "$DEST"
                    codesign --force --deep --sign - "$DEST" >/dev/null 2>&1
                    open "$DEST"
                    rm -rf "$(dirname "$0")"
                else
                    rm -rf "$DEST"; [ -e "$BAK" ] && mv "$BAK" "$DEST"; open "$DEST"
                fi
                """.write(to: script, atomically: true, encoding: .utf8)
                let p = Process()
                p.executableURL = URL(fileURLWithPath: "/bin/sh")
                p.arguments = [script.path]
                p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
                try p.run()
                NSApp.terminate(nil)
            } catch {
                phase = .failed(error.localizedDescription); showResult = true
            }
        }
    }

    private func run(_ exe: String, _ args: [String]) throws {
        let p = Process(); p.executableURL = URL(fileURLWithPath: exe); p.arguments = args
        try p.run(); p.waitUntilExit()
        if p.terminationStatus != 0 { throw UpdateError.msg("\(exe): код \(p.terminationStatus)") }
    }
}

struct UpdateUI: ViewModifier {
    @ObservedObject var u: Updater
    func body(content: Content) -> some View {
        content
            .task { if ProcessInfo.processInfo.environment["NO_UPDATE_CHECK"] == nil { u.check(manual: false) } }
            .alert("Есть обновление", isPresented: $u.askInstall) {
                Button("Установить и перезапустить") { u.install() }
                Button("Позже", role: .cancel) {}
            } message: {
                Text("Доступна версия \(u.manifest?.version ?? "") (у вас \(appVersion)).\n\n\(u.manifest?.notes ?? "")"
                     + (Updater.isTranslocated ? "\n\nПрограмма запущена из временной защищённой папки — новая версия встанет в «Программы»." : ""))
            }
            .alert(resultTitle, isPresented: $u.showResult) { Button("OK", role: .cancel) {} } message: { Text(resultText) }
            .overlay(alignment: .bottom) {
                if u.phase == .downloading || u.phase == .installing {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(u.phase == .downloading ? "Скачивание обновления…" : "Установка обновления…").font(.system(size: 11))
                    }
                    .padding(8).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8)).padding(10)
                }
            }
    }
    private var resultTitle: String {
        if case .failed = u.phase { return "Не удалось обновить" }
        return "Обновлений нет"
    }
    private var resultText: String {
        if case .failed(let s) = u.phase { return s }
        return "У вас последняя версия (\(appVersion))."
    }
}
