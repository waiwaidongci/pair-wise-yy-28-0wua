# 影视拍摄连续性管理

项目使用 Python 标准库、SQLite 和 `http.server` 管理非线性拍摄中的连续性。场次与镜头分别记录叙事顺序和拍摄顺序，角色、服装、道具、伤痕状态按叙事链检查，冲突可由调整方案或正式豁免处理，镜头只有在无未处理冲突时才能锁定。场次进入拍摄日前需通过整片预检：按叙事顺序比对相邻场次的首尾状态，跨场问题与场内问题共用调整方案和豁免流程，存在未处理问题的场次不能排期，拍摄日只有全部排期场次无阻断时才能放行。

## 运行与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8115`，页面 <http://127.0.0.1:8115>。首次启动创建“雨夜追踪”示例，其中拍摄顺序与叙事顺序相反，并生成一个场内伤痕回退冲突和一个 S01→S02 的跨场外套冲突，另建一个未排期的拍摄日。数据库和端口可分别用 `CONTINUITY_DB`、`PORT` 指定。

## 代码分层

- `database.py`：数据层，负责表结构、迁移、存取和事务，检查结论由此落库。
- `precheck.py`：检查逻辑层，只做判定不写库——元素规则判定、跨场首尾比对、场次/拍摄日阻断计算。
- `app.py` 与 `static/index.html`：页面与接口操作层，展示待处理问题、拍摄日状态和阻断原因。

## 连续性算法

每个元素选择一种规则：

- `stable`：沿叙事顺序状态必须一致。
- `monotonic`：使用 `numeric_value` 比较，数值不能下降，适合伤痕、污损或破坏程度。
- `allowed`：只有预先登记的状态转移才能通过。

检测按叙事顺序执行，与剪辑和拍摄顺序无关。场内检查比对同一场次内相邻镜头；整片预检把同一规则应用到相邻场次的首尾状态（前一场最后一个有状态的镜头对后一场第一个有状态的镜头），跨场冲突以 `scope='cross'` 写入同一张冲突表。调整方案必须由制片人或场记提出、由另一位审片人批准；批准后写入镜头状态并重新检查。也可以为确实需要保留的冲突写入豁免理由。锁定会再次检查场次，豁免之外的活跃冲突会阻止锁定，锁定后直接改状态会失败。

## 拍摄日预检与放行

- 制片或场记维护拍摄日、场次排期（含拍摄顺位）和放行状态；审片人只审核方案与豁免。
- 排期和放行前都会重检全片（场内 + 跨场）：场次有未处理问题（活跃且未豁免，无论场内还是跨场）时不能排期；拍摄日任一已排期场次存在未处理问题时不能放行，错误信息列出阻断原因。
- 已放行的拍摄日不能调整排期，需先撤销放行；放行后若出现新问题，看板会标记“需处理并重检”，处理完重检后可再次放行。

## 主要接口

- `POST /api/users`、`POST /api/productions`
- `POST /api/productions/{id}/scenes`、`POST /api/scenes/{id}/shots`
- `POST /api/productions/{id}/elements`、`POST /api/elements/{id}/transitions`
- `POST /api/shots/{id}/states`、`POST /api/scenes/{id}/check`
- `POST /api/conflicts/{id}/plans`、`POST /api/plans/{id}/review`
- `POST /api/conflicts/{id}/exemptions`
- `POST /api/shots/{id}/lock`
- `GET /api/productions/{id}/continuity`
- `POST /api/productions/{id}/precheck`、`GET /api/productions/{id}/board`
- `POST /api/productions/{id}/shoot-days`
- `POST /api/shoot-days/{id}/scenes`、`POST /api/shoot-days/{id}/unschedule`
- `POST /api/shoot-days/{id}/release`
