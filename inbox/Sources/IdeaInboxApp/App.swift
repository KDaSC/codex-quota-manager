import SwiftUI
import AppKit
import UniformTypeIdentifiers
import IdeaInboxCore

@MainActor final class InboxModel: ObservableObject {
    @Published var text = ""
    @Published var automatic = false
    @Published var attachedFiles: [IdeaAttachment] = []
    @Published var draftFolderPath: String?
    @Published var tasks: [IdeaTask] = []
    @Published var stopped = false
    @Published var working = false
    @Published var message: String?
    @Published var observation: CodexObservation?
    @Published var rootPath = ""
    @Published var connectionStatus = "尚未检查"
    private var store: InboxStore?
    private var worker: InboxWorker?
    private var timer: Task<Void, Never>?
    private var failures = 0
    private var started = false
    private var registeredRoot: String?

    func setQueueRegistered(_ root: String) { registeredRoot = root }

    init() {
        draftFolderPath = UserDefaults.standard.string(forKey: "IdeaInboxDraftFolder")
        let args = CommandLine.arguments
        let root: URL
        if let i = args.firstIndex(of: "--root"), args.indices.contains(i + 1) {
            root = URL(fileURLWithPath: args[i + 1], isDirectory: true)
        } else if let path = UserDefaults.standard.string(forKey: "IdeaInboxDataRoot") {
            root = URL(fileURLWithPath: path, isDirectory: true)
        } else if let path = Bundle.main.object(forInfoDictionaryKey: "IdeaInboxDataRoot") as? String {
            root = URL(fileURLWithPath: path, isDirectory: true)
        } else if let relative = Bundle.main.object(forInfoDictionaryKey: "IdeaInboxRelativeDataRoot") as? String {
            root = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent(relative).standardizedFileURL
        } else {
            root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/IdeaInbox")
        }
        do { try configure(root) } catch { message = error.localizedDescription }
    }
    private func configure(_ root: URL) throws {
        let newStore = try InboxStore(root: root)
        store = newStore; worker = InboxWorker(store: newStore); observation = nil
        rootPath = newStore.root.path
        reload()
    }
    private func reload() { tasks = store?.state.tasks.reversed() ?? []; stopped = store?.state.stopped ?? true }
    func start() async {
        guard registeredRoot == rootPath, !started else { return }; started = true
        await refresh()
        await pump()
    }
    func save() {
        guard let store else { message = "数据目录尚未打开，请选择可写目录。"; return }
        do {
            let folder = draftFolderPath.map { URL(fileURLWithPath: $0, isDirectory: true).resolvingSymlinksInPath().path }
            _ = try store.capture(text: text, automatic: automatic, workspacePath: folder, attachedFiles: attachedFiles)
            text = ""; attachedFiles = []; message = automatic ? "已保存，正在等待整理。" : "原话已保存。"; reload()
            if automatic { Task { await pump() } }
        } catch { message = error.localizedDescription }
    }
    func chooseDraftFolder() {
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "选择草稿位置"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        draftFolderPath = url.resolvingSymlinksInPath().standardizedFileURL.path
        UserDefaults.standard.set(draftFolderPath, forKey: "IdeaInboxDraftFolder")
        message = "新记录的草稿将保存到所选文件夹。"
    }
    func useInboxFolder() {
        draftFolderPath = nil
        UserDefaults.standard.removeObject(forKey: "IdeaInboxDraftFolder")
        message = "新记录将保存在本机收件箱的数据目录中。"
    }
    func addFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true; panel.prompt = "添加参考文件"
        panel.allowedContentTypes = ["txt", "md", "csv", "json", "log", "swift", "py", "js", "ts", "yaml", "yml"]
            .compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK else { return }
        do {
            let remaining = 8 - attachedFiles.count
            guard panel.urls.count <= remaining else { throw InboxError.message("每条想法最多附加 8 个文本文件。") }
            let newFiles = try panel.urls.map(readTextFile)
            let allFiles = attachedFiles + newFiles
            guard allFiles.reduce(0, { $0 + $1.name.utf8.count + $1.text.utf8.count }) <= 120_000 else {
                throw InboxError.message("所选文件合计太大（上限约 120 KB）；请少选一些或精简内容。")
            }
            attachedFiles = allFiles
            message = "已添加 \(newFiles.count) 个文本文件；原文件不会被修改。"
        } catch { message = error.localizedDescription }
    }
    private func readTextFile(_ url: URL) throws -> IdeaAttachment {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              attrs[.type] as? FileAttributeType == .typeRegular else {
            throw InboxError.message("只能添加普通文本文件：\(url.lastPathComponent)")
        }
        guard ((attrs[.size] as? NSNumber)?.intValue ?? Int.max) <= 120_000 else {
            throw InboxError.message("单个文件不能超过约 120 KB：\(url.lastPathComponent)")
        }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: 120_001) ?? Data()
        guard data.count <= 120_000 else {
            throw InboxError.message("文件读取期间变大，已停止：\(url.lastPathComponent)")
        }
        guard let contents = String(data: data, encoding: .utf8) else {
            throw InboxError.message("文件不是 UTF-8 文本：\(url.lastPathComponent)")
        }
        return IdeaAttachment(name: url.lastPathComponent, text: contents)
    }
    func removeFile(_ id: UUID) { attachedFiles.removeAll { $0.id == id } }
    func showInFinder(_ path: String) { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) }
    func openDataFolder() { NSWorkspace.shared.open(URL(fileURLWithPath: rootPath, isDirectory: true)) }
    func refresh() async {
        guard let worker, !working else { return }
        working = true; defer { working = false; reload(); schedule() }
        do {
            try await worker.refresh()
            observation = worker.observation
            failures = 0
            if let observed = observation, !observed.authenticated {
                connectionStatus = "需要在 Codex 登录"
                message = "已连接到 Codex，但当前没有可用的 ChatGPT 登录。"
            } else if let observed = observation,
                      !observed.models.contains(where: { $0.id == "gpt-6.1-sol" && $0.efforts.contains("medium") }) {
                connectionStatus = "目标模型暂不可用"
                message = "Codex 已连接；当前模型目录没有 GPT-6.1 Sol / Medium。"
            } else if observation?.quota == nil {
                connectionStatus = "已连接 · 额度待确认"
                message = "Codex 已连接，但没有读到有效额度；自动整理会继续等待。"
            } else {
                connectionStatus = "Codex 已连接"
                message = "已完成只读握手并读取模型与额度。"
            }
        } catch {
            failures += 1; observation = nil
            connectionStatus = error.localizedDescription.contains("未找到本机 Codex") ? "未找到 Codex" : "连接失败"
            message = error.localizedDescription
        }
    }
    func pump() async {
        guard registeredRoot == rootPath, let worker, let store, !working else { return }
        timer?.cancel(); working = true
        // Update only local in-memory state while a job is active; no extra RPCs.
        let progress = Task { [weak self] in
            while !Task.isCancelled, self?.working == true {
                self?.reload()
                do { try await Task.sleep(nanoseconds: 250_000_000) } catch { return }
            }
        }
        defer { progress.cancel() }
        repeat {
            let completedBefore = store.state.tasks.filter { $0.status == .completed }.count
            await worker.runNext()
            observation = worker.observation; message = worker.lastError; reload()
            if worker.lastError != nil { failures += 1 } else { failures = 0 }
            // Continue only after a completed job; never spin on waiting/unknown states.
            guard !store.state.stopped,
                  store.state.tasks.filter({ $0.status == .completed }).count > completedBefore,
                  let next = store.state.tasks.first(where: { $0.automatic && [.queued, .waitingQuota, .waitingConnection].contains($0.status) }),
                  next.status == .queued, worker.lastError == nil else { break }
        } while true
        working = false; reload(); schedule()
    }
    private func schedule() {
        timer?.cancel()
        if let store, (try? SharedQueueGate(root: store.root).managed) == true,
           !store.state.stopped,
           store.state.tasks.contains(where: { $0.automatic && [.queued, .waitingQuota, .waitingConnection].contains($0.status) }) {
            // This is only a local permit check. A missing permit never opens
            // an App Server connection or consumes a model turn.
            timer = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
                guard !Task.isCancelled else { return }
                await self?.pump()
            }
            return
        }
        guard let store, let date = ObservationSchedule.next(
            hasPending: store.state.tasks.contains(where: { $0.automatic && [.queued, .waitingQuota, .waitingConnection].contains($0.status) }),
            stopped: store.state.stopped, quota: observation?.quota ?? store.state.cachedQuota, failures: failures, now: Date()) else { return }
        timer = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(max(0, date.timeIntervalSinceNow) * 1_000_000_000)) }
            catch { return }
            guard !Task.isCancelled else { return }
            await self?.pump()
        }
    }
    func toggleStopped() {
        guard let store else { return }
        do {
            try SharedQueueGate(root: store.root).setPaused(!store.state.stopped)
            try store.setStopped(!store.state.stopped); reload()
            message = stopped ? "已阻止新任务；正在运行的回合将收到停止请求。" : "已允许自动整理。"
            if stopped { timer?.cancel() } else { Task { await pump() } }
        } catch { message = error.localizedDescription }
    }
    func cancel(_ id: UUID) {
        do { try store?.cancel(id); reload(); schedule() }
        catch { message = error.localizedDescription }
    }
    func reconcile(_ id: UUID) async {
        guard let worker, !working else { return }
        working = true; await worker.reconcile(id); message = worker.lastError
        working = false; reload(); schedule()
    }
    func saveRecoveredDraft(_ id: UUID) {
        guard let worker, !working else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "保存草稿到此处"
        guard panel.runModal() == .OK, let folder = panel.url else { return }
        Task {
            working = true
            do {
                try worker.saveRecoveredDraft(id, to: folder)
                message = "已保存此前读回的草稿；没有再次调用 Codex。"
            } catch { message = error.localizedDescription }
            working = false; reload(); schedule()
        }
    }
    func wake() {
        worker?.invalidateObservation(); observation = nil; timer?.cancel()
        Task { await refresh(); await pump() }
    }
    func chooseFolder() {
        guard !working else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "使用此目录"
        guard panel.runModal() == .OK, let root = panel.url else { return }
        if root.resolvingSymlinksInPath().path == rootPath { return }
        Task {
            working = true; timer?.cancel()
            do {
                let newStore = try InboxStore(root: root)
                await worker?.shutdown()
                registeredRoot = nil
                store = newStore; worker = InboxWorker(store: newStore); observation = nil; failures = 0
                rootPath = newStore.root.path
                UserDefaults.standard.set(rootPath, forKey: "IdeaInboxDataRoot")
                message = "已打开新的数据目录；原目录的数据保持原位。"
                working = false; reload(); await pump()
            }
            catch { message = error.localizedDescription; working = false; reload(); schedule() }
        }
    }
}

private struct GlassControl: ViewModifier {
    var prominent = false
    @ViewBuilder func body(content: Content) -> some View {
        if prominent { content.buttonStyle(.borderedProminent) }
        else { content.buttonStyle(.bordered) }
    }
}

private struct GlassGroup<Content: View>: View {
    @ViewBuilder var content: () -> Content
    @ViewBuilder var body: some View {
        if #available(macOS 26, *) { GlassEffectContainer(spacing: 12) { content() } }
        else { content() }
    }
}

struct InboxView: View {
    @ObservedObject var model: InboxModel
    @State private var selectedID: UUID?
    @Namespace private var selection
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    private var motion: Animation? { reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.84) }
    private var accent: Color { ManagerTheme.accent }

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 256)
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                header.padding(.horizontal, 24).padding(.vertical, 20)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        composer
                        if let message = model.message {
                            Label(message, systemImage: "info.circle")
                                .font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        detail
                    }.padding(24)
                }
                Divider()
                footer.padding(.horizontal, 24).padding(.vertical, 14)
            }
        }
        .frame(minWidth: 960, minHeight: 720)
        .background(ManagerTheme.canvas)
        .tint(accent)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: model.message)
        .task { await model.start(); if selectedID == nil { selectedID = model.tasks.first?.id } }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)) { _ in model.wake() }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 12) {
                ManagerAppIcon(size: 44)
                VStack(alignment: .leading, spacing: 3) {
                    Text("想法收件箱").font(.headline)
                    Text("留住一闪而过的灵感").font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack { Text("我的想法").font(.caption.weight(.semibold)).foregroundStyle(.secondary); Spacer(); Text("\(model.tasks.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(model.tasks) { task in
                        Button {
                            withAnimation(motion) { selectedID = task.id }
                        } label: {
                            VStack(alignment: .leading, spacing: 9) {
                                Text(String(task.text.prefix(48))).font(.body.weight(.medium)).lineLimit(2).multilineTextAlignment(.leading)
                                HStack(spacing: 6) {
                                    Circle().fill(task.status == .completed ? Color.green : accent).frame(width: 5, height: 5)
                                    Text(task.status.label).font(.caption).foregroundStyle(.secondary)
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(14)
                                .background {
                                    if selectedID == task.id {
                                        RoundedRectangle(cornerRadius: 16).fill(accent.opacity(colorScheme == .dark ? 0.24 : 0.12))
                                            .matchedGeometryEffect(id: "selection", in: selection)
                                    }
                                }
                        }.buttonStyle(.plain).accessibilityAddTraits(selectedID == task.id ? .isSelected : [])
                    }
                }.padding(2)
            }
            Spacer(minLength: 0)
            Label("本机保存 · 随时记录", systemImage: "internaldrive").font(.caption).foregroundStyle(.secondary)
        }.padding(22).background(ManagerTheme.sidebar)
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 6) {
                Text("把想法留下").font(.title2.weight(.semibold))
                Text("先把想到的写下来，整理可以稍后再做。").font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            GlassGroup {
                HStack(spacing: 12) {
                    if model.working { ProgressView().controlSize(.small).accessibilityLabel("正在处理") }
                    Button(action: model.toggleStopped) {
                        Label(model.stopped ? "允许整理" : "暂停整理", systemImage: model.stopped ? "play.fill" : "pause.fill")
                    }.modifier(GlassControl()).controlSize(.large)
                }
            }
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack { Label("此刻的想法", systemImage: "pencil.line").font(.subheadline.weight(.semibold)); Spacer(); Text("⌘ ↩ 保存").font(.caption).foregroundStyle(.tertiary) }
            TextField("想到什么，就写下来。", text: $model.text, axis: .vertical)
                .font(.body).textFieldStyle(.plain).lineLimit(4...8)
                .frame(minHeight: 80, alignment: .topLeading)
                .accessibilityLabel("当前想法")
            if !model.attachedFiles.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(model.attachedFiles) { file in
                            HStack(spacing: 5) {
                                Image(systemName: "doc.text")
                                Text(file.name).lineLimit(1)
                                Button { model.removeFile(file.id) } label: { Image(systemName: "xmark.circle.fill") }
                                    .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("移除 \(file.name)")
                            }
                            .font(.caption).padding(.horizontal, 9).padding(.vertical, 6)
                            .background(.primary.opacity(0.045), in: Capsule())
                        }
                    }
                }.scrollIndicators(.hidden)
            }
            GlassGroup {
                HStack(spacing: 12) {
                    Button(action: model.addFiles) { Label("从访达添加", systemImage: "paperclip") }
                        .modifier(GlassControl()).help("选择 UTF-8 文本作为参考资料；最多 8 个，合计约 120 KB。")
                    Spacer()
                    Button {
                        model.save()
                        withAnimation(motion) { selectedID = model.tasks.first?.id }
                    } label: { Label("保存想法", systemImage: "arrow.down.to.line") }
                        .keyboardShortcut(.return, modifiers: .command).modifier(GlassControl(prominent: true)).controlSize(.large)
                        .disabled(model.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            Toggle("保存后自动整理成草稿", isOn: $model.automatic)
                .toggleStyle(.checkbox).font(.callout)
            Divider()
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("草稿保存位置").font(.caption.weight(.semibold))
                    Text(model.draftFolderPath.map { "所选草稿文件夹：\($0)" } ?? "草稿保存在本机收件箱（数据目录见设置）")
                        .font(.caption2).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                }
                Spacer(minLength: 8)
                Button(action: model.chooseDraftFolder) { Label("选择文件夹", systemImage: "folder.badge.plus") }.modifier(GlassControl())
                if model.draftFolderPath != nil {
                    Button("收件箱") { model.useInboxFolder() }.modifier(GlassControl())
                }
            }
            Text(model.automatic
                 ? "自动整理会把原话和你选中的文本文件发送给 Codex，并使用账户额度。文件只作参考；项目文件夹只用于保存草稿。"
                 : "添加的 UTF-8 文本文件只在本机暂存；勾选自动整理后，才会连同原话发送给 Codex。原文件不会被改写。")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(18).background(ManagerTheme.surface, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(ManagerTheme.border))
    }

    @ViewBuilder private var detail: some View {
        if let task = model.tasks.first(where: { $0.id == selectedID }) {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Label(task.status.label, systemImage: task.status == .completed ? "checkmark.circle.fill" : "text.bubble")
                        .font(.headline).foregroundStyle(task.status == .completed ? Color.green : accent)
                    Spacer()
                    if task.status == .uncertain {
                        if task.draft != nil {
                            Button("另选位置保存草稿") { model.saveRecoveredDraft(task.id) }
                                .modifier(GlassControl()).disabled(model.working)
                        }
                        Button("核对原回合") { Task { await model.reconcile(task.id) } }.modifier(GlassControl()).disabled(model.working)
                    } else if ![.completed, .cancelled, .running, .preparing].contains(task.status) {
                        Button("取消整理") { model.cancel(task.id) }.buttonStyle(.borderless)
                    }
                }
                if let note = task.note { Text(note).font(.callout).foregroundStyle(.secondary) }
                Text(task.text).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                if let files = task.attachedFiles, !files.isEmpty {
                    Label("参考文件：\(files.map(\.name).joined(separator: "、"))", systemImage: "paperclip")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                Text("草稿位置：\(task.workspacePath ?? model.rootPath)")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                if let draft = task.draft {
                    Divider().padding(.vertical, 4)
                    Label("整理草稿", systemImage: "sparkles").font(.subheadline.weight(.semibold)).foregroundStyle(accent)
                    Text(draft).textSelection(.enabled).transition(.opacity)
                    if let path = task.draftPath {
                        Button { model.showInFinder(path) } label: { Label("在访达中显示草稿", systemImage: "arrow.up.forward.app") }
                            .modifier(GlassControl())
                    }
                }
                if let op = task.operation { Text("已请求：\(op.model) · \(op.effort)").font(.caption).foregroundStyle(.secondary) }
            }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(ManagerTheme.surface, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(ManagerTheme.border))
            .id(task.id).transition(.opacity)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: task.status)
        } else {
            ManagerEmptyState(title: "还没有选中记录", message: "保存一个新想法，或在左侧选择已有记录。", symbol: "lightbulb")
                .frame(minHeight: 140)
        }
    }

    private var footer: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Label(model.connectionStatus, systemImage: model.connectionStatus == "Codex 已连接" ? "checkmark.circle" : "bolt.horizontal.circle")
                    .font(.caption).foregroundStyle(model.connectionStatus == "Codex 已连接" ? .green : .secondary)
                if let quota = model.observation?.quota {
                    HStack(spacing: 14) {
                        ForEach(quota.windows) { window in
                            Text("\(window.durationMinutes == 300 ? "五小时" : window.durationMinutes == 10080 ? "每周" : "额度")剩余 \(Int(window.remainingPercent))%")
                                .font(.caption.monospacedDigit()).foregroundStyle(window.remainingPercent <= 10 ? .orange : .secondary)
                        }
                    }
                    Text("上次检查结果 · 派发前会重新核验额度").font(.caption2).foregroundStyle(.tertiary)
                } else { Text("额度未确认 · 仍可本机记录").font(.caption2).foregroundStyle(.tertiary) }
            }
            Spacer(minLength: 8)
            GlassGroup {
                HStack(spacing: 12) {
                    Button { Task { await model.refresh() } } label: { Label("检查 Codex", systemImage: "arrow.triangle.2.circlepath") }.disabled(model.working).modifier(GlassControl())
                }
            }
        }
    }
}

private struct InboxSettingsView: View {
    @ObservedObject var model: InboxModel

    var body: some View {
        Form {
            Section("Codex 连接") {
                LabeledContent("状态", value: model.connectionStatus)
                Button("重新检查 Codex 与额度") { Task { await model.refresh() } }
                    .disabled(model.working)
                Text("应用启动时会通过本机 Codex App Server 读取登录、模型目录和额度。此握手不会发送你的想法，也不会请求模型生成内容。")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Section("本机数据") {
                Text("这里保存想法、队列状态、额度快照和你主动添加的文本副本。它不是项目文件夹；草稿位置可在记录时单独选择。")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                LabeledContent("数据目录", value: model.rootPath.isEmpty ? "尚未打开" : model.rootPath)
                    .textSelection(.enabled)
                HStack {
                    Button("在访达中打开") { model.openDataFolder() }.disabled(model.rootPath.isEmpty)
                    Button("更改数据目录…") { model.chooseFolder() }.disabled(model.working)
                }
            }
        }
        .formStyle(.grouped)
        .padding(20)
        .frame(width: 560)
    }
}

struct InboxMenu: View {
    @ObservedObject var model: InboxModel
    @ObservedObject var dashboard: DashboardModel
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        Button("打开任务管家") { openWindow(id: "inbox"); NSApp.activate(ignoringOtherApps: true) }
        Text(dashboard.runnerStatus)
        Button(dashboard.globalAuto ? "关闭全自动续跑" : "开启全自动续跑") {
            Task { await dashboard.action("set_auto_all", ["enabled":!dashboard.globalAuto]) }
        }.disabled(dashboard.busy || !dashboard.hasSnapshot)
        Button("暂停全部派发") { Task { await dashboard.action("pause") } }.disabled(dashboard.busy)
        Button(model.stopped ? "允许自动整理" : "暂停自动整理", action: model.toggleStopped)
        Divider()
        Button("退出") { NSApp.terminate(nil) }
        .task { await dashboard.refresh() }
    }
}

@main struct IdeaInboxApp: App {
    @StateObject private var model = InboxModel()
    @StateObject private var dashboard = DashboardModel()
    var body: some Scene {
        WindowGroup("Codex 任务管家", id: "inbox") { ControlCenterView(inbox: model, model: dashboard) }
            .defaultSize(width: 1060, height: 780).defaultLaunchBehavior(.presented)
        Settings { InboxSettingsView(model: model) }
        MenuBarExtra("Codex 任务管家", systemImage: "camera.macro") { InboxMenu(model: model, dashboard: dashboard) }
    }
}
