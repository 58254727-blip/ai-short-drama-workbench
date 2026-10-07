# Original Duanju Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. 实现者使用独立上下文，不访问火宝源码或旧短剧数据。

**Goal:** 独立实现一个能贯通编导、资产、分镜、单GPU生成、声音字幕校核及整集导出的本地中文制作工作台。

**Architecture:** Python本地服务负责持久数据、任务和媒体适配器，原创浏览器界面使用同源API。一个GPU工作槽与最多两个CPU工作槽分开执行；保存真实提交ID和时间戳，重启先协调外部任务而不重复生成。

**Tech Stack:** Python 3.12、SQLite、标准库HTTP与测试、HTML/CSS/ES Modules、FFmpeg/FFprobe、ComfyUI本地HTTP接口。额外ASR或文本服务通过显式配置适配，不打包模型或账号。

**Spec:** `docs/superpowers/specs/2026-10-07-original-workbench-design.md`

## Global Constraints

- 代码只在 `F:\OpenSource\duanju-workbench-original`；运行数据在独立可配置目录并被Git忽略。
- 不读取、复制或翻译火宝代码、文档、样式及资源；不搬入两部剧的真实内容或配置。
- 默认只监听 `127.0.0.1`，默认新端口 `8766`，被占用则清楚报错，不停止其他服务。
- GPU槽为1，CPU槽最大2；不清空、打断或卸载其他Comfy任务，不升级驱动。
- 不配置模型仍可人工编辑及导入；模型与ASR未配置必须返回明确阻塞，不能生成模拟业务结果。
- 原生对白完整性和说话人是约束；字幕、识别或解码测试不能冒充人工听审/成片验收。
- 没有新付费调用、自动发布、自动迁移旧剧；真实H3试验另在安全空档确认。

## Review Focus

1. 跨作品/跨集镜头、素材与任务ID：必须拒绝且不改变原记录（任务1、3、6）。
2. 重启发生在提交结果尚未持久化时：不能再次提交；显示待协调状态（任务2、3）。
3. Unicode路径、超出数据根的路径、失效/变更源文件：不得泄露文件或继续使用错误素材（任务1、4）。
4. 缺音轨、超时、数字/否定词错读、字幕越界或截对白：明确失败/待审，不用补帧掩盖（任务4、5）。
5. 本地网页跨站写入、配置秘钥、备份与Git发布：限制来源和文件范围，不向第三方泄露（任务6、7）。

## 共同接口与数据约定

- ID为UUID字符串；时间为UTC ISO 8601；画面/音频时间使用非负毫秒整数。
- `project -> episode -> scene/shot`；资产属于project，角色、场景、道具分类型；镜头可引用同作品的版本化资产。
- 镜头字段：`id, episode_id, order, revision, story_job, start_state, action, end_state, transition, dialogue[{speaker_id,text}], screen_duration_ms, generation_duration_ms, asset_version_ids`。
- 更新镜头要求当前`revision`；不匹配返回409。资产版本和候选文件不可原地覆盖。
- 任务字段：`id, project_id, episode_id, shot_id, kind, state, payload, external_id, attempt, timestamps, error, result_asset_id`。
- 状态：`draft/ready/queued/submitting/running/needs_reconcile/candidate/selected/failed/cancelled`；等待人工审看与选片不等同发布验收。
- 响应成功返回JSON数据；错误返回 `{error:{code,message,details}}`，不得附秘钥或任意服务器文件内容。
- API：`/api/projects`、`/api/projects/{p}/episodes`、`/api/episodes/{e}/shots`、`/api/projects/{p}/assets`、`/api/jobs`、`/api/jobs/{j}`、`/api/episodes/{e}/timeline`、`/api/settings/status`。媒体通过资产ID访问，不接受任意绝对路径。
- 仅提供项目范围的导入、导出、备份恢复；请求必须包含归属和版本，不沿用火宝API。

### Task 1: 原创数据、资产、版本与备份

**Files:** `workbench/store.py`, `workbench/domain.py`, `workbench/assets.py`, `tests/test_store.py`, `tests/test_assets.py`。

**Interfaces:** `Store(db_path: Path)`；`create_project(title:str)->dict`、`create_episode(project_id:str,title:str)->dict`、`save_shot(episode_id:str,payload:dict,expected_revision:int|None)->dict`、`import_asset(project_id:str,path:Path,kind:str,rights:dict)->dict`、`select_candidate(shot_id:str,asset_id:str,expected_revision:int)->dict`、`export_project(project_id:str)->dict`、`restore_project(bundle:dict)->dict`。调用错误使用`DomainError(code,status,message)`。

- [ ] 写行为测试：两作品各一集，跨集更新被拒绝且快照不变；旧revision返回409；重复导入相同SHA复用文件但不改变选片；外部路径、失效源拒绝；恢复已有ID冲突不覆盖。
- [ ] 运行 `python -m unittest tests.test_store tests.test_assets -v`，先观察目标行为未实现的失败。
- [ ] 实现SQLite外键、原子写入、不可变资产版本、流式哈希及数据根路径校验；备份只含明确选中项目数据，不含秘钥。
- [ ] 运行以上测试及 `python -m unittest discover -s tests -v`；通过后提交本任务代码与测试。

### Task 2: 持久队列、恢复与耗时记录

**Files:** `workbench/queue.py`, `workbench/worker.py`, `tests/test_queue.py`。

**Interfaces:** 使用Task1的Store；`Queue.enqueue(scope:dict,kind:str,payload:dict)->dict`、`claim(resource:str)->dict|None`、`record_external(job_id:str,external_id:str)->None`、`finish(job_id:str,result:dict)->None`、`fail(job_id:str,code:str,message:str)->None`、`recover()->list[dict]`。资源分类`gpu`与`cpu`，Worker注册`handler(kind, job)->dict`。

- [ ] 写行为测试：GPU同时最多1、CPU最多2；CPU任务不等待GPU任务结束；重开保留队列；submitting且无external_id转needs_reconcile、不重发；同缺陷两次后拒绝等价重试。
- [ ] 运行 `python -m unittest tests.test_queue -v`，观察失败后实现事务领取、租约、恢复和实际阶段计时。
- [ ] 重试保留源版本和失败分类，改变方案创建新版本；取消未提交任务不操作其他外部队列。
- [ ] 跑全测试并提交；不能用真实旧GPU任务做队列测试。

### Task 3: H3与文本生成适配

**Files:** `workbench/adapters/comfy.py`, `workbench/adapters/text.py`, `workbench/workflows.py`, `tests/test_adapters.py`。

**Interfaces:** `ComfyAdapter(endpoint:str,timeout_s:float)`；`check()->dict`、`submit(workflow:dict,client_id:str)->str`、`status(prompt_id:str)->dict`、`collect(prompt_id:str)->list[dict]`。`TextAdapter.generate(messages:list[dict],schema:dict|None)->dict`。工作流绑定显式node/input映射，不拼接任意Python或shell代码。

- [ ] 本地HTTP测试服务验证：忙队列不提交；缺node、401、超时、非JSON均真实失败；只追踪本任务prompt_id；提交不明确时不自动重发；跨集生成结果不得落入其他集。
- [ ] 运行 `python -m unittest tests.test_adapters -v`，观察失败后实现官方公开接口及超时、进度、错误处理。
- [ ] 输入、输出文件使用Task1资产接口；AI建议作为待采纳版本，不直接覆盖当前剧情或对白；文本服务未配置时人工流程仍可用。
- [ ] 测试独立配置没有本机盘符、演员、模型名或凭证。全测试通过后提交。

### Task 4: 媒体校核与真实导出

**Files:** `workbench/media.py`, `workbench/exporter.py`, `tests/test_media.py`, `tests/test_export.py`。

**Interfaces:** `probe(path:Path)->dict`、`decode_check(path:Path)->dict`、`trim(source:Path,in_ms:int,out_ms:int,dest:Path)->dict`、`export_episode(scope:dict,timeline:list[dict],subtitle_path:Path|None)->dict`。时间轴项为选片资产ID及实际in/out，不接受循环填时。

- [ ] 用FFmpeg生成虚构动态测试视频：两条实际拼接并解码、字幕UTF-8及空格路径；无音轨、超源时长、源哈希改变、全静态误当动态必须明确待审/拒绝。
- [ ] 运行 `python -m unittest tests.test_media tests.test_export -v`，观察失败后实现可核验媒体处理。
- [ ] 依据流兼容性选择拼接方式；需要重编码明确记录。切点保护标记的对白范围，不自动砍掉半句。
- [ ] 真实输出记录时长、帧率、音轨、哈希、解码状态及未人工审看的项；全测试通过后提交。

### Task 5: 声音校核、字幕与编导检查

**Files:** `workbench/transcripts.py`, `workbench/subtitles.py`, `workbench/story_checks.py`, `tests/test_dialogue.py`, `tests/test_subtitles.py`。

**Interfaces:** `review_transcript(expected:list[dict],actual:list[dict])->dict`、`save_cues(episode_id:str,cues:list[dict],revision:int)->dict`、`write_srt(cues:list[dict],path:Path)->None`、`review_story(episode:dict)->list[dict]`。字幕cue为`start_ms,end_ms,text,speaker_id,source_asset_id`。

- [ ] 测试否定词/数字遗漏、额外话语、未知说话人、重叠或越界字幕、跨集source_asset均能被标记；识别相近字不能自动算真错读。
- [ ] 运行对应测试观察失败；实现原文与实录对照、SRT导出及逐条编辑。ASR使用明确配置的离线适配，不自动下载模型；未配置显示缺项。
- [ ] 编导检查只给可定位建议（动机、因果、场景目标、转场、时序），不能保证爆火或伪称听看过。
- [ ] 全测试通过后提交；记录“机器线索”和“用户听审”分别的状态。

### Task 6: 原创中文工作台与API贯通

**Files:** `workbench/server.py`, `web/index.html`, `web/styles.css`, `web/app.js`, `tests/test_api.py`, `tests/frontend.test.mjs`。

**Interfaces:** 按共同API连接Task1–5；项目/集选择、剧本与分镜编辑、资产参考、候选预览、队列、声音字幕校核、时间轴、导出及设置状态。

- [ ] 先测同源API：跨站写入被拒绝、非法ID与字段返回结构化错误、秘钥不回传、媒体仅允许本项目资产；模型缺失不阻断人工保存。
- [ ] 新界面依照 `frontend-app-builder` 技能独立设计，不使用火宝布局、样式或图标。各操作必须真实落库、有加载/错误/空状态及中文提示。
- [ ] `python -m unittest tests.test_api -v`、`node --test tests/frontend.test.mjs`；浏览器贯通虚构作品创建、镜头修改、导入选片、排队、字幕、导出并重开验证。
- [ ] 修复本轮引入的问题，跑全测试后提交；截图只能证明已实际操作的流程。

### Task 7: 复核、发布准备与GitHub

**Files:** `README.md`, `docs/ORIGIN.md`, `docs/VERIFICATION.md`, `.gitignore`, `tools/check_public_files.py`, `.github/workflows/tests.yml`。

- [ ] 保存原创需求、公开接口来源与提交记录；只用通用组件，不复制火宝内容。逐项核对引入依赖及许可，原创声明不扩展到第三方。
- [ ] 发布检查拒绝数据库、模型权重、媒体、真实演员/剧本、秘钥、推理日志及本机配置；测试缺陷样本只含虚构内容。
- [ ] 完整验证：`python -m unittest discover -s tests -v`、`node --test tests/frontend.test.mjs`、`python tools/check_public_files.py`及真实媒体导出/解码；准备可复现启动说明。
- [ ] 独立复核一次关键流程；不得把适配器协议测试说成真实H3或人工听审通过。实际H3验证另确认安全空档与用户许可。
- [ ] GitHub上传前恢复登录，确认仓库名称/公开或私有/源码许可；先核对暂存文件，再推送并核验远端commit。此步骤未完成不称已上传。

## 官方接口依据（2026-10-07核验）

- ComfyUI：<https://docs.comfy.org/development/comfyui-server/comms_routes>。仅使用所需公开接口，不调用清队列、interrupt或free。
- FFmpeg拼接：<https://ffmpeg.org/ffmpeg-formats.html#concat>。兼容性不满足时不能冒称无损复制。
- FFmpeg字幕：<https://ffmpeg.org/ffmpeg-filters.html#subtitles-1>。本机版本能力以实际检查为准。

## 当前交接

计划已写入，尚未实施。建议独立上下文实现：先数据/API和队列，再媒体适配与界面，最后一次集成复核；不派多个代理跟踪同一GPU队列。待用户确认计划范围与执行方式后开始。
