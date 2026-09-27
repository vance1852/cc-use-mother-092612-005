# 扫码科普展项布设与巡检基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

## 标牌布设与巡检模块

`signage.py` 在基础层之上实现二维码标牌的全生命周期管理，解决闭市巡检发现的"牌随摊走、内容错位、统计失真"问题：

- **标牌身份**：`signs` 为每块实体标牌分配终身不变的 `sign_id`，布设变更不影响身份；
- **布设窗口**：`sign_deployments` 以生效窗口 `[effective_from, effective_to)` 关联专区、展项与内容摘要，同一标牌至多一条生效布设；
- **换位交接**：移出方 `POST /transfers` 发起即让旧位置失效，接收方 `POST /transfers/confirm` 确认后新布设才生效，两方不能是同一人；历史扫描永远归属原布设，不做迁移；
- **离线回传**：`POST /scan-batches` 按 `(场所, 设备, 设备序号)` 去重——同序号同内容为重放（不计数），同序号不同内容为分叉（拒绝并审计）；失效位置、未知标牌、摘要不符分别进入对应巡检队列；回传只保留设备与标牌信息，不保存任何可还原个人浏览轨迹的原始标识；
- **巡检闭环**：任务领取（`POST /inspections/claim`）后带明确过期时间，过期可被重新领取；结案（`POST /inspections/resolve`）必须由领取人在有效期内引用现场更换、重新张贴或误报复核三类证据之一；发生在结案之前的迟到事件只作为补充挂在已结案任务下，不推翻受保护的结论，结案之后的新异常开启新任务；
- **运营查询**：`GET /signs/placement` 查标牌当前应在位置，`GET /signs/anomaly-origin` 查异常起点，`GET /scan-statistics` 给出纯聚合计数的匿名统计口径，`GET /inspections` 按队列与状态过滤任务；
- **重启保持**：全部状态落在 SQLite，未结巡检在应用重启后保持原处理状态与认领过期时间。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、标牌巡检模块、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、标牌巡检规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，并完整走一遍标牌登记、布设、离线回传分拣、巡检领取、换位交接、模拟重启后结案的全流程，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
