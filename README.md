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
