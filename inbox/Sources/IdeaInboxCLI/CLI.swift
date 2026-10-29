import Foundation
import IdeaInboxCore

private struct Options {
    var values: [String]
    mutating func flag(_ name: String) -> Bool {
        guard let i = values.firstIndex(of: name) else { return false }
        values.remove(at: i)
        return true
    }
    mutating func option(_ name: String) throws -> String? {
        guard let i = values.firstIndex(of: name) else { return nil }
        guard values.indices.contains(i + 1) else { throw InboxError.message("参数缺少值。") }
        let value = values.remove(at: i + 1)
        values.remove(at: i)
        return value
    }
}

@main struct CLI {
    static func printJSON<T: Encodable>(_ value: T) throws {
        FileHandle.standardOutput.write(try InboxJSON.encode(value))
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
    @MainActor static func main() async {
        do {
            var args = Options(values: Array(CommandLine.arguments.dropFirst()))
            let rootPath = try args.option("--root") ?? FileManager.default.currentDirectoryPath + "/运行数据"
            guard !args.values.isEmpty else {
                print("用法：idea-inbox --root 数据目录 capture --text 原话 [--auto]；list；show UUID；stop；allow；cancel UUID")
                return
            }
            let command = args.values.removeFirst()
            let store = try InboxStore(root: URL(fileURLWithPath: rootPath, isDirectory: true))
            switch command {
            case "work":
                let worker = InboxWorker(store: store)
                await worker.runNext()
                await worker.shutdown()
                try printJSON(store.state)
            case "inspect":
                let connection = CodexConnection(executable: try CodexConnection.locateExecutable())
                do {
                    try await connection.connect()
                    try await printJSON(connection.observe())
                    await connection.close()
                } catch { await connection.close(); throw error }
            case "simulate":
                let primary = Double(try args.option("--primary-used") ?? "20")
                let secondary = Double(try args.option("--secondary-used") ?? "30")
                let age = Double(try args.option("--age") ?? "0")
                guard let primary, let secondary, let age, primary.isFinite, secondary.isFinite, age.isFinite,
                      args.values.count == 1, let id = UUID(uuidString: args.values[0]) else {
                    throw InboxError.message("模拟参数无效；请提供任务 UUID 和数字额度。")
                }
                let now = Date()
                let quota = QuotaSnapshot(accountKey: "simulation", collectedAt: now.addingTimeInterval(-age), source: .simulation,
                                          windows: [QuotaWindow(usedPercent: primary, durationMinutes: 300, resetsAt: nil),
                                                    QuotaWindow(usedPercent: secondary, durationMinutes: 10080, resetsAt: nil)])
                struct Simulation: Encodable { let source: String; let decision: DispatchDecision; let networkRequests: Int }
                try printJSON(Simulation(source: "simulation", decision: QuotaPolicy.decide(state: store.state, task: store.task(id), quota: quota, now: now), networkRequests: 0))
            case "capture":
                let automatic = args.flag("--auto")
                let text: String
                if args.flag("--stdin") {
                    guard let input = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8) else {
                        throw InboxError.message("输入必须是 UTF-8 文本。")
                    }
                    text = input
                } else {
                    guard let input = try args.option("--text") else { throw InboxError.message("请提供 --text 或 --stdin。") }
                    text = input
                }
                guard args.values.isEmpty else { throw InboxError.message("存在未识别参数。") }
                try printJSON(store.capture(text: text, automatic: automatic))
            case "list": try printJSON(store.state)
            case "show", "cancel":
                guard args.values.count == 1, let id = UUID(uuidString: args.values[0]) else {
                    throw InboxError.message("请提供有效的任务 UUID。")
                }
                if command == "cancel" { try store.cancel(id) }
                try printJSON(store.task(id))
            case "stop", "allow":
                try store.setStopped(command == "stop")
                try printJSON(store.state)
            default: throw InboxError.message("未知命令。")
            }
        } catch {
            FileHandle.standardError.write(Data("错误：\(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
}
