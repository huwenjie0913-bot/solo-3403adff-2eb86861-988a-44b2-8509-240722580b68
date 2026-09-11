# Braille Duplex Preflight & Layout Service

供盲文出版人员使用的双面点字（interpoint）版面预检与排版 REST API。
本机运行，不依赖任何外部服务。

技术栈：Python 3.11 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 · SQLite

## 功能概览

- **输入**：Unicode Braille（U+2800–U+28FF）或 BRF（北美盲文 ASCII）文本，
  附段落 / 标题 / 表格区间；`\n` 分行，`\f` 为显式换页（锁定页界）。
- **配置**：纸张尺寸、页边距、装订边与装订线、点距、方距（cell 距）、行距、
  interpoint 翻转方式（`page_flip` 左右翻 / `top_flip` 上下翻）、
  双面错点偏移与最小凸点间距、8 点盲文开关。
- **预检**：把每个点位换算为纸面物理坐标（mm），检查
  非法字符、8 点禁用、版心越界、行列超限、正反面镜像错误、
  两面凸点间距不足（撞点）。每页输出正反面点阵、撞点来源与装订方向。
  原始单元格与显式换页一律保留，不擅自改写盲文内容。
- **排版**：在允许的断行、空行和跨页范围内重排，遵守标题同页、
  段首末最少行数（孤行/寡行控制）、表格不拆分、锁定页界；
  返回分页映射与无法满足的规则；多套方案按（页数, 违例数, 改动量）排序。
- **版本**：确认方案后另存版本（快照含配置、方案、映射、内容哈希）。
- **导出**：PEF（Portable Embosser Format）、凸点/凹点 SVG 校样、
  JSON 追溯记录（含每点坐标与撞点来源）。
- **压印机编译**：压印机配置声明每行方数、每页行数、6/8 点能力、
  输入编码（BRF / UTF-8 Braille）、换页控制与双面送纸方式。读取已确认
  版本，核对版面网格与设备容量后生成可发送的字节流；原生双面机按纸张
  配对输出，单面机拆为正、背两次作业并按 page_flip / top_flip 计算页序、
  倒序规则与重新装纸方向。编译不改写盲文单元或显式换页；无法编码字符、
  8 点不兼容、行列超限、缺失背面、控制字节冲突均带页/行/列/来源返回。
  每个批次绑定版本与设备配置快照，可下载各次作业文件与 JSON 工单
  （纸张顺序、装纸朝向、字节校验值、单元映射）；回读接口重新解析字节流，
  确认页序、换页与单元映射仍与原版本一致。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
# 数据库默认 ./braille_preflight.db，可用环境变量覆盖：
# BRAILLE_DB_URL=sqlite:////tmp/x.db uvicorn app.main:app
```

测试：

```bash
python3 -m pytest tests/ -q
```

## 几何模型

坐标系：纸面左上角为原点，x 向右、y 向下，单位 mm。

- 正面按逻辑网格排布：cell (row, col) 原点为
  `(版心左 + col·方距, 版心上 + row·行距)`，cell 内点位偏移为点距的整数倍
  （点 1/2/3 在左列，点 4/5/6 在右列；8 点时点 7/8 在第四行）。
- 背面使用同一逻辑网格，**先在背面自身逻辑坐标系中施加 interpoint 错点偏移，
  再按翻转方式镜像**到物理坐标：
  - `page_flip`（左右翻）：`(x, y) → (W − x, y)`
  - `top_flip`（上下翻）：`(x, y) → (x, H − y)`
- 背面版心 `printable_back` 为正面版心经同一镜像变换的结果；装订线因此
  自动出现在背面物理内侧（书脊侧）。偏移先于镜像施加，保证逻辑版心内的
  合法点位镜像后仍落在 `printable_back` 内。
- 撞点检查：同一纸张上正、反两面凸点的物理距离小于
  `interpoint.min_separation_mm` 即报 `dot_collision`（含双方点位来源）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/jobs` | 创建作业（BRF 自动转 Unicode；校验结构与配置） |
| GET | `/jobs` / `/jobs/{id}` | 列表 / 详情 |
| DELETE | `/jobs/{id}` | 删除作业及其报告、排版运行与版本 |
| POST | `/jobs/{id}/preflight` | 运行预检并保存报告 |
| GET | `/jobs/{id}/preflight` | 最近一份预检报告 |
| GET | `/jobs/{id}/sheets/{n}/dots` | 第 n 张纸正反面全部凸点坐标 |
| POST | `/jobs/{id}/layout` | 重排，返回排序后的候选方案 |
| GET | `/jobs/{id}/layout` / `/layout-runs/{id}` | 排版运行列表 / 详情 |
| POST | `/jobs/{id}/versions` | 确认方案（或原始分页）另存版本 |
| GET | `/jobs/{id}/versions` / `/versions/{id}` | 版本列表 / 快照 |
| GET | `/versions/{id}/export/pef` | PEF 导出（卷 1 正面、卷 2 背面） |
| GET | `/versions/{id}/export/svg?sheet=&layer=&style=` | SVG 校样：`layer=front/back/overlay`，`style=emboss/deboss` |
| GET | `/versions/{id}/export/trace` | JSON 追溯记录 |
| POST | `/embossers` | 注册压印机配置（方数/行数、6/8 点、编码、换页、双面送纸） |
| GET | `/embossers` / `/embossers/{id}` | 压印机列表 / 详情 |
| DELETE | `/embossers/{id}` | 删除配置（已有批次保留配置快照） |
| POST | `/versions/{id}/compile` | 编译版本为字节流（`embosser_id` 或内联 `embosser`）；失败 422 含错误定位 |
| GET | `/compile-batches?version_id=` / `/compile-batches/{id}` | 编译批次列表 / 详情（含工单） |
| GET | `/compile-batches/{id}/ticket` | 下载 JSON 工单（纸张顺序、装纸朝向、校验值、单元映射） |
| GET | `/compile-batches/{id}/files` | 批次各次作业文件列表 |
| GET | `/compile-files/{id}/download` | 下载某次作业的 BRF / UTF-8 Braille 字节流 |
| POST | `/compile-batches/{id}/readback` | 回读校验：重新解析字节流并对照原版本 |

## 示例

```bash
# 创建 BRF 作业（含第 2 页背面）
curl -X POST localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "name": "demo", "source_format": "brf",
  "content": "A1B,\nA1B,\nA1B,\fA1B,",
  "structure": {"blocks": []},
  "config": {"paper": {"width_mm": 100, "height_mm": 80},
             "margins": {"top_mm": 5, "bottom_mm": 5, "left_mm": 5, "right_mm": 5, "binding_mm": 0}}
}'

curl -X POST localhost:8000/jobs/1/preflight          # -> ok: true
curl -X POST localhost:8000/jobs/1/layout -H 'Content-Type: application/json' \
     -d '{"params": {"cells_per_line": 30, "lines_per_page": 20}}'
curl -X POST localhost:8000/jobs/1/versions -H 'Content-Type: application/json' \
     -d '{"layout_run_id": 1, "solution_index": 0, "note": "approved"}'
curl localhost:8000/versions/1/export/pef
curl 'localhost:8000/versions/1/export/svg?sheet=1&layer=overlay'
curl localhost:8000/versions/1/export/trace

# 注册压印机并编译版本（单面机：正、背两个作业文件）
curl -X POST localhost:8000/embossers -H 'Content-Type: application/json' -d '{
  "name": "simplex-6dot",
  "config": {"cells_per_line": 30, "lines_per_page": 20, "supports_8dot": false,
             "input_encoding": "brf", "page_break": "form_feed", "line_ending": "crlf",
             "duplex_mode": "simplex_manual"}
}'
curl -X POST localhost:8000/versions/1/compile -H 'Content-Type: application/json' \
     -d '{"embosser_id": 1, "note": "first run"}'
curl localhost:8000/compile-batches/1/ticket        # JSON 工单
curl -OJ localhost:8000/compile-files/1/download    # 正面作业字节流
curl -X POST localhost:8000/compile-batches/1/readback   # 回读校验 -> ok: true
```

## 结构说明

- 内容行编号：全部内容按 `\f` 分页、`\n` 分行后拉平，从 0 开始连续编号；
  结构区间 `line_start`/`line_end` 为半开区间。
- 未覆盖的行自动按空行分段为段落；空行作为内容原样保留。
- 段落重排只在空白单元处断行（可用 `break_at_blank_only=false` 关闭）；
  超长单词硬断并记违例。重排不增删改任何盲文单元。
- 排版约束参数见 `POST /jobs/{id}/layout` 的 `LayoutParams`
  （标题同页行数、段首末最少行数、表格不拆分、锁定页界、页数预算等）。
- 无法满足的规则在运行级 `unsatisfiable_rules` 与方案级
  `violations` / `unsatisfied_rules` 中分别报告。

## 压印机编译说明

- **设备配置**（`EmbosserConfig`）：`cells_per_line` / `lines_per_page`
  （设备容量）、`supports_8dot`、`input_encoding`（`brf` 北美盲文 ASCII /
  `unicode_braille` UTF-8 盲文）、`page_break`（`form_feed` 每页后发 FF；
  `line_advance` 用空行补齐整页走纸；`none` 设备自行换页）、`line_ending`
  （lf/crlf/cr）、`duplex_mode`（`native_duplex` / `simplex_manual`）、
  `pad_missing_back`（缺失背面补空白页而非报错）、`extra_control_bytes`
  （额外保留控制字节，单元编码与之冲突即报错）。
- **单面机装纸规则**（假设出纸面朝上、后印者压在先印者之上）：
  - `page_flip`：整叠纸绕垂直轴左右翻转后重新装纸，叠序被整体反转，
    背面作业按**正序**输出（`reload.direction=left_right`）。
  - `top_flip`：从出纸叠顶部逐张取纸、每张上下翻滚重新进纸，先印的后出，
    背面作业按**倒序**输出（`reload.direction=top_bottom`）。
- **编译错误**：`unencodable_character`、`eight_dot_not_supported`、
  `line_too_long`、`page_overflow`、`missing_back_side`、
  `control_byte_conflict`，均带 `page`/`row`/`col` 与 `source`
  （`duplex` / `front` / `back` 作业次）。
- **批次与工单**：批次绑定 `version_id` 与设备配置快照；工单含
  `paper_order`（纸张顺序）、`reload`（装纸朝向与倒序规则）、每文件
  `sha256` 与 `cell_mapping`（文件页 → 版本页/源行的单元映射）。
- **回读**：按批次的设备快照重新解析字节流，逐页逐行逐单元对照原版本，
  并核对换页标记与文件校验值；不一致处返回页、行、列与来源。
