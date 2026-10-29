import SwiftUI
import Foundation

private struct ControlSnapshot: Decodable {
    struct Draft: Decodable, Identifiable {
        let text: String, fingerprint: String
        let threadId: String?, canAdd: Bool, known: Bool, ignored: Bool
        var id: String { fingerprint }
    }
    struct Chat: Decodable, Identifiable {
        let id: String, name: String, projectName: String, status: String
        let archived: Bool, draft: Draft?, statusObservedAt: Double?
        let autoSelected: Bool
    }
    struct Window: Decodable, Identifiable {
        let kind: String, remaining: Double
        var id: String { kind }
    }
    struct Settings: Decodable {
        let reserve: Int, globalAuto: Bool, selectedAutoCount: Int
        let autoHealth: String, autoHealthCheckedAt: Double?
    }
    struct Job: Decodable, Identifiable {
        let id: String, threadId: String, kind: String, prompt: String, status: String, reason: String
    }
    struct Item: Decodable, Identifiable {
        let seq: Int, kind: String, target: String, status: String, reason: String
        var id: Int { seq }
    }
    let threads: [Chat], unboundDrafts: [Draft], draftsAvailable: Bool
    let inventoryError: String?, windows: [Window], settings: Settings
    let tasks: [Job], items: [Item], paused: Bool, runnerRunning: Bool, nextOffset: Int
}

private enum BackendFailure: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(value) = self { return value }; return nil }
}
private enum ControlBackend {
    static func run(_ args: [String], input: Data? = nil) async throws -> Data {
        try await Task.detached(priority: .userInitiated) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            let packaged = Bundle.main.resourceURL?.appendingPathComponent("backend")
            let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            process.currentDirectoryURL = packaged.flatMap { FileManager.default.fileExists(atPath: $0.appendingPathComponent("codex_resume").path) ? $0 : nil } ?? source
            let cli = CommandLine.arguments
            let queueArgs: [String]
            if let i = cli.firstIndex(of: "--queue-state-dir"), cli.indices.contains(i + 1) {
                queueArgs = ["--state-dir", cli[i + 1]]
            } else { queueArgs = [] }
            process.arguments = ["-m", "codex_resume"] + queueArgs + args
            let output = Pipe(), errors = Pipe(), stdin = Pipe()
            process.standardOutput = output; process.standardError = errors; process.standardInput = stdin
            try process.run()
            if let input { stdin.fileHandleForWriting.write(input) }
            try? stdin.fileHandleForWriting.close()
            let result = output.fileHandleForReading.readDataToEndOfFile()
            let err = errors.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                let lines = (String(data: err, encoding: .utf8) ?? "").split(separator: "\n")
                throw BackendFailure.message(lines.last.map(String.init) ?? "无法连接本机调度器")
            }
            return result
        }.value
    }
}
@MainActor final class DashboardModel: ObservableObject {
    @Published fileprivate var snapshot: ControlSnapshot?
    @Published var error: String?
    @Published var busy = false
    @Published var reserve = 20.0
    @Published var reservePending = false
    private var offset = 0
    private var refreshing = false
    private var reserveTask: Task<Void, Never>?
    private var observed: [String: (String, Double)] = [:]
    var globalAuto: Bool { snapshot?.settings.globalAuto ?? false }
    var hasSnapshot: Bool { snapshot != nil }
    var detectionWarning: String? {
        guard let snapshot, snapshot.runnerRunning, !snapshot.paused,
              snapshot.settings.globalAuto || snapshot.settings.selectedAutoCount > 0 else { return nil }
        if snapshot.settings.autoHealth == "unavailable" {
            return "自动检测暂不可用，请检查 Codex 连接；后台会再次尝试。"
        }
        guard snapshot.settings.autoHealth == "ok", let checked = snapshot.settings.autoHealthCheckedAt else {
            return "后台已启动，正在等待首次检测。"
        }
        let age = Date().timeIntervalSince1970 - checked
        if age > 180 || age < -30 { return "自动检测状态暂未更新，请检查 Codex 连接。" }
        return nil
    }
    var runnerStatus: String {
        guard let snapshot else { return "正在读取状态" }
        guard snapshot.runnerRunning else { return "后台未运行" }
        guard !snapshot.paused else { return "后台已暂停" }
        if detectionWarning != nil {
            return snapshot.settings.autoHealth == "unknown" ? "等待首次检测" : "检测需要留意"
        }
        return "后台调度中"
    }
    func refresh() async {
        guard !refreshing else { return }; refreshing = true
        defer { refreshing = false }
        do {
            let data = try await ControlBackend.run(["desktop", "snapshot", "--offset", String(offset)])
            let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
            let result = try decoder.decode(ControlSnapshot.self, from: data)
            offset = result.nextOffset
            for chat in result.threads { if let at = chat.statusObservedAt { observed[chat.id] = (chat.status, at) } }
            observed = observed.filter { Date().timeIntervalSince1970 - $0.value.1 < 60 }
            snapshot = result
            if !reservePending { reserve = Double(result.settings.reserve) }
        } catch { self.error = error.localizedDescription }
    }
    fileprivate func status(_ chat: ControlSnapshot.Chat) -> String {
        let raw = observed[chat.id]?.0 ?? "unknown"
        return ["running":"执行中", "idle":"空闲", "waiting_quota":"等待额度", "needs_attention":"需要处理", "unknown":"状态待确认"][raw] ?? "状态待确认"
    }
    @discardableResult func action(_ name: String, _ payload: [String: Any] = [:]) async -> Bool {
        guard !busy else { return false }; busy = true
        defer { busy = false }
        do {
            let data = try JSONSerialization.data(withJSONObject: ["action":name, "payload":payload])
            _ = try await ControlBackend.run(["desktop", "action"], input: data)
            error = nil; await refresh(); return true
        } catch { self.error = error.localizedDescription; return false }
    }
    func setReserve(_ value: Double) {
        reserve = value; reservePending = true; reserveTask?.cancel()
        reserveTask = Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            let saved = await action("set_reserve", ["value":Int(value)])
            reservePending = false
            if !saved { reserve = Double(snapshot?.settings.reserve ?? 20) }
            await refresh()
        }
    }
}
// Shared visual primitives keep all three workspaces consistent with macOS appearance.
struct ManagerCard<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 14, content: content)
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(ManagerTheme.surface, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(ManagerTheme.border))
    }
}

struct ManagerEmptyState: View {
    let title: String
    let message: String
    let symbol: String
    var body: some View {
        VStack(spacing: 12) {
            ManagerAppIcon(size: 72)
                .padding(14).background(ManagerTheme.sidebar, in: RoundedRectangle(cornerRadius: 28))
            Text(title).font(.headline)
            Text(message).font(.callout).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
        }.padding(28).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct ManagerBadge: View {
    let text: String
    var color: Color = .secondary
    var body: some View {
        Text(text).font(.caption.weight(.medium))
            .padding(.horizontal, 8).padding(.vertical, 4)
            .foregroundStyle(color).background(color.opacity(0.10), in: Capsule())
            .fixedSize()
    }
}

struct ControlCenterView: View {
    @ObservedObject var inbox: InboxModel
    @ObservedObject var model: DashboardModel
    @State private var selected: String?
    @State private var filter = ""
    @State private var optionalText = ""
    @State private var optionalThread = ""
    @State private var registeredRoot = ""
    @State private var workspace = Workspace.chats
    @State private var searchCollapsedProjects: Set<String> = []
    @AppStorage("manager.collapsedProjects.v1") private var collapsedProjectsJSON = "[]"
    @AppStorage("manager.draftsExpanded.v1") private var draftsExpanded = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private enum Workspace: String, CaseIterable, Identifiable {
        case chats = "项目与聊天", optionalTasks = "可选任务", inbox = "想法收件箱"
        var id: String { rawValue }
        var symbol: String {
            switch self {
            case .chats: "square.stack.3d.up"
            case .optionalTasks: "leaf"
            case .inbox: "tray"
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            if let error = model.error { notice(error, color: .red) }
            brandBar
            Divider().overlay(ManagerTheme.border)
            Group {
                switch workspace {
                case .chats: dashboard
                case .optionalTasks: optionalPage
                case .inbox: InboxView(model: inbox)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)

        }
        .frame(minWidth: 960, minHeight: 720)
        .background(ManagerTheme.canvas)
        .tint(ManagerTheme.accent)
        .task {
            await registerInbox()
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
        .onChange(of: inbox.rootPath) { Task { await registerInbox() } }
        .onChange(of: filter) { searchCollapsedProjects.removeAll() }
    }

    private func registerInbox() async {
        guard !inbox.rootPath.isEmpty, registeredRoot != inbox.rootPath else { return }
        if await model.action("register_inbox", ["root":inbox.rootPath]) {
            registeredRoot = inbox.rootPath; inbox.setQueueRegistered(inbox.rootPath); await inbox.start(); await inbox.pump()
        }
    }

    private func notice(_ message: String, color: Color = .orange) -> some View {
        Label(message, systemImage: "exclamationmark.triangle")
            .font(.callout).foregroundStyle(color).textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12).background(color.opacity(0.08))
    }

    private var dashboard: some View {
        VStack(spacing: 16) {
            controls
            if let warning = model.detectionWarning { notice(warning) }
            if let error = model.snapshot?.inventoryError { notice(error) }
            if model.snapshot?.draftsAvailable == false { notice("草稿缓存不可读取，请在 Codex 中核对。") }
            HSplitView {
                chatList.frame(minWidth: 280, idealWidth: 320, maxWidth: 400)
                chatDetail.frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
            .background(ManagerTheme.surface, in: RoundedRectangle(cornerRadius: 14))
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(ManagerTheme.border))
        }.padding(20)
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("项目与聊天").font(.title2.weight(.semibold))
                    Text("额度中断后，在原聊天继续工作。").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                ManagerBadge(text: schedulerLabel, color: schedulerColor)
                Button { Task { await model.action("start") } } label: {
                    Label("开始后台调度", systemImage: "play.fill")
                }.buttonStyle(.borderedProminent).disabled(model.busy || model.reservePending)
                Button { Task { await model.action("pause") } } label: {
                    Label("暂停", systemImage: "pause.fill")
                }.disabled(model.busy).help("阻止后续派发；关闭窗口不会暂停后台。")
            }
            ManagerCard {
                HStack(alignment: .center, spacing: 14) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("自动续跑全部额度中断的聊天").font(.headline)
                        Text(model.globalAuto ? "已开启：自动检测本机聊天，恢复后逐个继续。" : "已关闭：可在每个聊天中单独开启。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle("自动续跑全部额度中断的聊天", isOn: Binding(
                        get: { model.globalAuto },
                        set: { value in Task { await model.action("set_auto_all", ["enabled":value]) } }))
                        .labelsHidden().disabled(model.busy || !model.hasSnapshot || model.reservePending)
                }
                Divider()
                HStack(alignment: .top, spacing: 24) {
                    VStack(alignment: .leading, spacing: 10) {
                        Label("账户剩余额度", systemImage: "gauge.with.dots.needle.50percent").font(.subheadline.weight(.semibold))
                        HStack(spacing: 24) {
                            quotaWindow("primary", title: "五小时")
                            quotaWindow("secondary", title: "每周")
                        }
                    }.frame(maxWidth: .infinity)
                    Divider()
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Label("最终保留", systemImage: "shield.lefthalf.filled").font(.subheadline.weight(.semibold))
                            Spacer()
                            Text("\(Int(model.reserve))%").font(.title3.weight(.semibold).monospacedDigit())
                        }
                        Slider(value: Binding(get: { model.reserve }, set: { model.setReserve($0) }), in: 10...40, step: 10)
                            .disabled(model.busy).accessibilityLabel("最终保留额度")
                        HStack {
                            Text("10%"); Spacer(); Text(model.reservePending ? "正在保存…" : "按 10% 调整"); Spacer(); Text("40%")
                        }.font(.caption).foregroundStyle(.secondary)
                    }.frame(width: 255)
                }.fixedSize(horizontal: false, vertical: true)
                Divider()
                HStack(alignment: .top, spacing: 20) {
                    Label("普通任务 > \(Int(model.reserve) + 10)%", systemImage: "text.bubble")
                    Label("可选任务 ≥ \(Int(model.reserve) + 15)%", systemImage: "checklist")
                    Spacer(minLength: 0)
                    Text("关闭窗口后，后台仍会运行").foregroundStyle(.secondary)
                }.font(.caption)
                Text("两个额度窗口均须满足门槛，派发前重新核验。额外 10% 为收尾消耗估计，实际可能不同。")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var schedulerLabel: String {
        model.runnerStatus
    }
    private var schedulerColor: Color {
        if model.detectionWarning != nil { return .orange }
        return model.snapshot?.runnerRunning == true && model.snapshot?.paused == false ? .green : .secondary
    }

    private func quotaWindow(_ kind: String, title: String) -> some View {
        let window = model.snapshot?.windows.first { $0.kind == kind }
        return VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).foregroundStyle(.secondary)
                Spacer()
                Text(window.map { "\(Int($0.remaining))%" } ?? "—")
                    .font(.system(size: 28, weight: .semibold, design: .rounded).monospacedDigit())
            }
            ProgressView(value: window?.remaining ?? 0, total: 100)
                .tint((window?.remaining ?? 0) <= model.reserve + 10 ? .orange : ManagerTheme.accent)
                .accessibilityLabel("\(title)剩余额度")
            Text(window == nil ? "额度待确认" : "剩余可用")
                .font(.caption).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity)
    }

    private var brandBar: some View {
        HStack(spacing: 12) {
            ManagerAppIcon(size: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text("Codex 任务管家").font(.headline)
                Text("草稿 · 续跑 · 想法").font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 24)
            Picker("工作区", selection: $workspace) {
                ForEach(Workspace.allCases) { page in
                    Label(page.rawValue, systemImage: page.symbol).tag(page)
                }
            }.pickerStyle(.segmented).frame(width: 390)
        }.padding(.horizontal, 24).padding(.vertical, 12)
            .background(ManagerTheme.surface)
    }

    private var chatList: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Label("我的聊天", systemImage: "bubble.left.and.bubble.right").font(.headline)
                    Spacer()
                    Text("\(filtered.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                TextField("搜索项目或聊天", text: $filter).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("搜索项目或聊天")
            }.padding(16)
            Divider()
            if filtered.isEmpty && unboundDrafts.isEmpty {
                ManagerEmptyState(title: model.snapshot == nil ? "正在读取聊天" : (filter.isEmpty ? "暂无聊天" : "没有匹配的聊天"),
                                  message: filter.isEmpty ? "聊天加载后会按项目显示在这里。" : "试试其他项目名称或聊天标题。", symbol: "bubble.left.and.bubble.right")
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        if !draftChats.isEmpty || !unboundDrafts.isEmpty {
                            DisclosureGroup(isExpanded: $draftsExpanded) {
                                VStack(spacing: 3) {
                                    ForEach(draftChats) { chat in chatButton(chat, pinned: true) }
                                    ForEach(unboundDrafts) { draft in
                                        VStack(alignment: .leading, spacing: 5) {
                                            Label("新聊天草稿", systemImage: "square.and.pencil").font(.caption.weight(.semibold))
                                            Text(draft.text).font(.caption).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                                            draftIgnore(draft).font(.caption)
                                        }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                                            .background(ManagerTheme.surface.opacity(0.6), in: RoundedRectangle(cornerRadius: 9))
                                    }
                                }.padding(.top, 6)
                            } label: {
                                HStack {
                                    Label("草稿置顶", systemImage: "pin.fill").font(.subheadline.weight(.semibold))
                                    Spacer()
                                    Text("\(draftChats.count + unboundDrafts.count)").font(.caption.monospacedDigit())
                                }.foregroundStyle(ManagerTheme.apricot)
                            }.padding(10).background(ManagerTheme.draftSurface, in: RoundedRectangle(cornerRadius: 12))
                        }
                        HStack {
                            Text("项目").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            Spacer()
                            Button { setAllProjects(expanded: true) } label: { Image(systemName: "arrow.down.right.and.arrow.up.left") }
                                .help("全部展开").accessibilityLabel("全部展开项目")
                            Button { setAllProjects(expanded: false) } label: { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                                .help("全部折叠").accessibilityLabel("全部折叠项目")
                        }.buttonStyle(.borderless).padding(.horizontal, 8).padding(.top, 12)
                        ForEach(groupNames, id: \.self) { group in
                            DisclosureGroup(isExpanded: projectExpansion(group)) {
                                VStack(spacing: 2) {
                                    ForEach(projectChats(group)) { chat in chatButton(chat) }
                                }.padding(.top, 4)
                            } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: "folder").foregroundStyle(ManagerTheme.accent)
                                    Text(group).font(.subheadline.weight(.medium)).lineLimit(1).help(group)
                                    Spacer(minLength: 2)
                                    Text("\(projectChats(group).count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                                }.padding(.vertical, 5).contentShape(Rectangle())
                            }.padding(.horizontal, 8)
                        }
                    }.padding(10)
                }
            }
        }.background(ManagerTheme.sidebar)
    }

    private var filtered: [ControlSnapshot.Chat] {
        (model.snapshot?.threads ?? []).filter { filter.isEmpty || $0.name.localizedCaseInsensitiveContains(filter) || $0.projectName.localizedCaseInsensitiveContains(filter) }
    }
    private var draftChats: [ControlSnapshot.Chat] { filtered.filter { $0.draft != nil } }
    private var unboundDrafts: [ControlSnapshot.Draft] {
        (model.snapshot?.unboundDrafts ?? []).filter { filter.isEmpty || $0.text.localizedCaseInsensitiveContains(filter) }
    }
    private var groupNames: [String] { Array(Set(filtered.map(\.projectName))).sorted() }
    private func projectChats(_ name: String) -> [ControlSnapshot.Chat] {
        let chats = filtered.filter { $0.projectName == name }
        // Partition preserves the account's recency ordering within both groups.
        return chats.filter { $0.draft != nil } + chats.filter { $0.draft == nil }
    }
    private var collapsedProjects: Set<String> {
        Set((try? JSONDecoder().decode([String].self, from: Data(collapsedProjectsJSON.utf8))) ?? [])
    }
    private func projectExpansion(_ group: String) -> Binding<Bool> {
        Binding(get: {
            filter.isEmpty ? !collapsedProjects.contains(group) : !searchCollapsedProjects.contains(group)
        }, set: { expanded in
            if filter.isEmpty {
                var names = collapsedProjects
                if expanded { names.remove(group) } else { names.insert(group) }
                saveCollapsedProjects(names)
            } else {
                if expanded { searchCollapsedProjects.remove(group) } else { searchCollapsedProjects.insert(group) }
            }
        })
    }
    private func saveCollapsedProjects(_ names: Set<String>) {
        if let data = try? JSONEncoder().encode(names.sorted()), let value = String(data: data, encoding: .utf8) { collapsedProjectsJSON = value }
    }
    private func setAllProjects(expanded: Bool) {
        if filter.isEmpty { saveCollapsedProjects(expanded ? [] : Set(groupNames)) }
        else { searchCollapsedProjects = expanded ? [] : Set(groupNames) }
    }
    private func chatButton(_ chat: ControlSnapshot.Chat, pinned: Bool = false) -> some View {
        Button {
            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.15)) { selected = chat.id }
        } label: {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: chat.draft != nil ? "square.and.pencil" : "bubble.left")
                    .foregroundStyle(chat.draft != nil ? ManagerTheme.apricot : ManagerTheme.accent)
                    .frame(width: 17).padding(.top, 2)
                VStack(alignment: .leading, spacing: 5) {
                    Text(chat.name).font(.callout.weight(selected == chat.id ? .semibold : .regular))
                        .foregroundStyle(.primary).lineLimit(1)
                    HStack(spacing: 5) {
                        if pinned { Text(chat.projectName).lineLimit(1); Text("·") }
                        Text(model.status(chat) + (chat.archived ? " · 已归档" : "")).lineLimit(1)
                        Spacer(minLength: 0)
                        if let draft = chat.draft {
                            Circle().fill(draft.known ? Color.secondary : ManagerTheme.apricot).frame(width: 5, height: 5)
                        }
                    }.font(.caption2).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }.padding(.horizontal, 10).padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(selected == chat.id ? ManagerTheme.selected : .clear, in: RoundedRectangle(cornerRadius: 9))
                .contentShape(Rectangle())
        }.buttonStyle(.plain).help(chat.projectName + " / " + chat.name)
            .accessibilityLabel(chat.name)
            .accessibilityValue((chat.draft != nil ? "有草稿，" : "") + chat.projectName + "，" + model.status(chat))
            .accessibilityAddTraits(selected == chat.id ? .isSelected : [])
    }

    @ViewBuilder private var chatDetail: some View {
        if let chat = model.snapshot?.threads.first(where: { $0.id == selected }) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(chat.projectName).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                        Text(chat.name).font(.title2.weight(.semibold)).textSelection(.enabled)
                        HStack {
                            ManagerBadge(text: model.status(chat))
                            if chat.archived { ManagerBadge(text: "已归档") }
                        }
                        HStack(spacing: 10) {
                            Button("打开原聊天", systemImage: "arrow.up.forward.app") { Task { await model.action("open_chat", ["threadId":chat.id]) } }
                        }.padding(.top, 4)
                        Toggle("这个聊天额度中断后自动续跑", isOn: Binding(
                            get: { chat.autoSelected },
                            set: { value in Task { await model.action("set_auto_chat", ["threadId":chat.id, "enabled":value]) } }))
                            .disabled(chat.archived || model.busy || model.globalAuto)
                        if model.globalAuto {
                            Text("全自动已开启；关闭全自动后可单独选择。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Divider()
                    if let draft = chat.draft {
                        ManagerCard {
                            Label("输入框草稿", systemImage: "square.and.pencil").font(.headline)
                            Text(draft.text.isEmpty ? "包含无法安全转发的内容" : draft.text)
                                .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                            if !draft.canAdd { Text("此草稿含不支持的内容、过长或未关联聊天，请在 Codex 中处理。") .font(.callout).foregroundStyle(.orange) }
                            HStack {
                                Button("加入任务") { Task { await model.action("add_draft", ["threadId":chat.id, "fingerprint":draft.fingerprint]) } }
                                    .buttonStyle(.borderedProminent).disabled(!draft.canAdd || draft.known || chat.archived || model.busy)
                                draftIgnore(draft)
                            }
                            Text("仅在你选择加入后进入队列；发送前会再次核对原草稿。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    VStack(alignment: .leading, spacing: 12) {
                        Label("已加入的任务", systemImage: "list.bullet.clipboard").font(.headline)
                        let jobs = (model.snapshot?.tasks ?? []).filter { $0.threadId == chat.id }
                        let watches = (model.snapshot?.items ?? []).filter { $0.kind == "chat" && $0.target == chat.id }
                        if jobs.isEmpty && watches.isEmpty {
                            Text("尚未加入任务。可选择上方草稿，或将此聊天加入额度续跑。")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                        ForEach(jobs) { job in jobRow(job) }
                        ForEach(watches) { item in
                            VStack(alignment: .leading, spacing: 6) {
                                HStack { Text("额度续跑").font(.subheadline.weight(.medium)); ManagerBadge(text: statusLabel(item.status)) }
                                if !item.reason.isEmpty { Text(item.reason).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                            }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(24)
            }
        } else {
            ManagerEmptyState(title: "选择一个聊天", message: "在左侧查看项目与聊天，\n选择后可核对草稿和管理续跑任务。", symbol: "bubble.left.and.text.bubble.right")
        }
    }

    private func draftIgnore(_ draft: ControlSnapshot.Draft) -> some View {
        Button(draft.ignored ? "恢复待处理" : (draft.known ? "已加入任务" : "暂不处理")) {
            Task { await model.action(draft.ignored ? "restore_draft" : "ignore_draft", ["fingerprint":draft.fingerprint]) }
        }.disabled(model.busy || (draft.known && !draft.ignored))
    }

    private var optionalPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 5) {
                Text("可选任务").font(.title2.weight(.semibold))
                Text("先保存，再选择加入队列。正常待办与未处理草稿完成后才会启动。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            HSplitView {
                ScrollView {
                    ManagerCard {
                        Label("新建可选任务", systemImage: "square.and.pencil").font(.headline)
                        Picker("目标聊天", selection: $optionalThread) {
                            Text("请选择聊天").tag("")
                            ForEach((model.snapshot?.threads ?? []).filter { !$0.archived }) { chat in Text("\(chat.projectName) / \(chat.name)").tag(chat.id) }
                        }
                        Text("任务内容").font(.subheadline.weight(.medium))
                        ZStack(alignment: .topLeading) {
                            TextEditor(text: $optionalText).font(.body).padding(6)
                                .frame(minHeight: 150).accessibilityLabel("可选任务内容")
                            if optionalText.isEmpty {
                                Text("写下空闲时希望完成的事…").foregroundStyle(.tertiary).padding(12).allowsHitTesting(false)
                            }
                        }.background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.primary.opacity(0.12)))
                        Button {
                            Task { if await model.action("add_optional", ["threadId":optionalThread, "text":optionalText]) { optionalText = "" } }
                        } label: { Label("保存可选任务", systemImage: "tray.and.arrow.down") }
                            .buttonStyle(.borderedProminent)
                            .disabled(optionalThread.isEmpty || optionalText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                        Text("保存不会启动。你还需要在右侧选择「加入队列」。")
                            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }.padding(.trailing, 16)
                }.frame(minWidth: 370, idealWidth: 430)
                VStack(alignment: .leading, spacing: 14) {
                    HStack {
                        Text("已保存的可选任务").font(.headline)
                        Spacer()
                        Text("\(optionalJobs.count) 条").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }.padding(.horizontal, 18)
                    if optionalJobs.isEmpty {
                        ManagerEmptyState(title: "还没有可选任务", message: "在左侧选择聊天并写下内容，\n保存后会出现在这里。", symbol: "checklist")
                    } else {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 12) {
                                ForEach(optionalJobs) { job in ManagerCard { jobRow(job) } }
                            }.padding(.horizontal, 18)
                        }
                    }
                }.padding(.top, 4).frame(minWidth: 360, maxWidth: .infinity, maxHeight: .infinity)
            }
        }.padding(24)
    }

    private var optionalJobs: [ControlSnapshot.Job] { (model.snapshot?.tasks ?? []).filter { $0.kind == "optional" } }

    private func statusLabel(_ status: String) -> String {
        ["stored":"已保存 · 未入队", "queued":"已加入队列", "running":"执行中", "completed":"已完成", "cancelled":"已取消",
         "needs_attention":"需要处理", "uncertain":"发送待核对", "watching":"正在观察", "waiting_quota":"等待额度",
         "waiting_connection":"等待连接", "waiting_drafts":"等待处理草稿", "waiting_normal":"等待正常任务",
         "waiting_inbox":"等待收件箱", "paused":"已暂停", "stopped":"已停止", "dispatching":"正在派发",
         "failed":"执行失败", "changed":"内容已变化", "settled":"已结束", "resumed":"已续跑",
         "checking":"正在核对", "preparing":"正在准备", "busy":"执行槽占用", "finished":"已结束" ][status] ?? status
    }

    private func jobRow(_ job: ControlSnapshot.Job) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let chat = model.snapshot?.threads.first(where: { $0.id == job.threadId }) {
                Text(chat.name).font(.caption).foregroundStyle(.secondary).lineLimit(1).help(chat.name)
            }
            Text(job.prompt).lineLimit(4).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            ManagerBadge(text: statusLabel(job.status), color: job.status == "completed" ? .green : .secondary)
            if !job.reason.isEmpty { Text(job.reason).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
            HStack {
                if job.status == "uncertain" {
                    Button("核对已完成结果") { Task { await model.action("reconcile_task", ["id":job.id]) } }
                        .disabled(model.busy)
                        .help("只读核对原聊天中是否已有完成结果，不会重新发送任务。")
                }
                if job.kind == "optional" && job.status == "stored" {
                    Button("选择加入队列") { Task { await model.action("select_optional", ["id":job.id]) } }.disabled(model.busy || model.reservePending)
                }
                if ["stored", "queued", "needs_attention"].contains(job.status) {
                    Button("取消任务") { Task { await model.action("cancel_task", ["id":job.id]) } }.disabled(model.busy)
                }
            }
        }.padding(.vertical, 4)
    }
}
