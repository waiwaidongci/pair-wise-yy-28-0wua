# 影视拍摄连续性管理

项目使用 Python 标准库、SQLite 和 `http.server` 管理非线性拍摄中的连续性。场次与镜头分别记录叙事顺序和拍摄顺序，角色、服装、道具、伤痕状态按叙事链检查；检测同时覆盖**单场内相邻镜头**和**叙事顺序相邻场次的首尾状态（跨场衔接）**。冲突可由调整方案或正式豁免处理，镜头只有在无未处理冲突时才能锁定。

## 整片预检与排期放行

- 按叙事顺序逐对元素比较：单场内沿镜头叙事顺序比较；跨场时取前场最后一个与后场第一个带状态的镜头比较，沿用同一套元素规则。
- 跨场问题挂在叙事下游场次上，与单场问题一起进入该场次的放行门禁。
- 制片或场记维护拍摄日、场次顺序和放行状态；**场次存在未处理（未豁免）的单场或跨场问题时，不能排期也不能放行**。
- 跨场问题与单场问题一样，通过“制片/场记提调整方案 → 审片人批准”或“审片人豁免”处理；处理后整片重检，阻断状态随之解除或重新出现。
- 审片人只处理风险（审方案、批豁免），不能维护拍摄日、排期和放行。
- 页面集中展示待处理问题、拍摄日状态和每个场次的阻断原因。

## 运行与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8115`，页面 <http://127.0.0.1:8115>。首次启动创建“雨夜追踪”示例：S01 与 S02 叙事相邻、拍摄顺序相反，形成一个跨场伤痕回退冲突；S01 已排入拍摄日 Day1，S02 被预检挡下。数据库和端口可分别用 `CONTINUITY_DB`、`PORT` 指定。

## 连续性算法

每个元素选择一种规则：

- `stable`：沿叙事顺序状态必须一致。
- `monotonic`：使用 `numeric_value` 比较，数值不能下降，适合伤痕、污损或破坏程度。
- `allowed`：只有预先登记的状态转移才能通过。

检测按叙事顺序整片执行（单场 + 跨场），与剪辑和拍摄顺序无关。调整方案必须由制片人或场记提出、由另一位审片人批准；批准后写入镜头状态并整片重新检查。也可以为确实需要保留的冲突写入豁免理由。锁定镜头与排期/放行都会再次检查，豁免之外的活跃冲突会阻止操作，锁定后直接改状态会失败。

## 分层

- `continuity.py`：纯检查逻辑，输入场次/镜头/元素状态数据，输出单场与跨场问题，不碰数据库和 HTTP。
- `database.py`：数据承载（拍摄日、场次排期与放行、冲突/方案/豁免）和门禁。
- `app.py` 与 `static/index.html`：页面操作（预检、拍摄日维护、排期、放行、风险处理）。

## 主要接口

- `POST /api/users`、`POST /api/productions`
- `POST /api/productions/{id}/shoot-days`（制片/场记）
- `POST /api/productions/{id}/scenes`、`POST /api/scenes/{id}/shots`
- `POST /api/productions/{id}/elements`、`POST /api/elements/{id}/transitions`
- `POST /api/shots/{id}/states`、`POST /api/scenes/{id}/check`
- `POST /api/productions/{id}/precheck`：整片预检，返回场次状态、拍摄日状态和阻断原因
- `POST /api/scenes/{id}/schedule`：排入/移出拍摄日（未处理问题阻断）
- `POST /api/scenes/{id}/release`：预检后放行或撤回放行（body 中 `release:false` 撤回）
- `POST /api/conflicts/{id}/plans`、`POST /api/plans/{id}/review`
- `POST /api/conflicts/{id}/exemptions`
- `POST /api/shots/{id}/lock`
- `GET /api/productions/{id}/continuity`、`GET /api/scenes/{id}/conflicts`
