# 扫码科普展项布设与巡检基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

在基础层之上，本项目还实现了**实体二维码标牌**的布设、交接、离线回传与巡检闭环：

- **标牌身份终身不变**：每块实体标牌分配独立 `plaque_id`，标牌移动、换位不改变身份；二维码内容直接编码该身份。
- **带生效窗口的布设记录**：布设把标牌关联到专区、展项、内容摘要文本与内容摘要哈希，并记录 `effective_from / effective_to`。同一时刻每块标牌最多一条生效记录；旧窗口关闭后保留，历史扫描继续指向旧布设，不做迁移。
- **两步交接才成立**：换位先由移出方 `release`、再由接收方 `receive`（不能同人）。两步齐备时旧布设的 `effective_to` 立即写入、新生效窗口开启；交接完成前，旧位置仍然有效。
- **离线回传分类**：终端按 `(设备, 当日伪名, 事件序号)` 回传。同序号同载荷判为**重放**（只计数），同序号不同载荷判为**分叉**（只登记两个哈希，冲突载荷不落库）。事件分别判为 `ok / stale_position / unknown_plaque / digest_mismatch`，后三类自动进入对应巡检队列。
- **隐私最小化**：设备原始序号从不存储，使用每站点、每 UTC 日随机盐生成伪名，跨日不可关联；统计表只给当日去重伪名数，运营无法还原个人浏览轨迹。
- **巡检领取与受保护结论**：工单领取有明确过期时间（默认 30 分钟），过期自动退回队列；结案必须引用现场更换（`on_site_replacement`）、重新张贴（`repost`）或误报复核（`false_alarm_review`）证据。结案后结论受保护：扫描时间早于结案的迟到回传只能作为补充（`case_supplements`），不能重开工单；结案之后新发生的异常开新单。
- **可恢复**：未结巡检状态、领取过期时刻均持久化在 SQLite，应用重启后保持原处理状态。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、标牌布设与巡检服务、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、标牌全链路和端到端验收测试。

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

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点、专区与展项参考资料，完成标牌登记、布设、交接换位，模拟正常/重放/分叉/未知标牌/摘要不符回传、巡检领取与凭证据结案、迟到事件补充，核对幂等回执、匿名口径与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

### 标牌相关接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /plaques` | 登记实体标牌身份（幂等 `request_id`） |
| `POST /plaques/deploy` | 建立首个带生效窗口的布设（专区、展项、摘要文本、摘要哈希） |
| `POST /relocations` | 发起换位交接申请 |
| `POST /relocations/handover` | `action=release` 移出方释放，`action=receive` 接收方签收 |
| `POST /relocations/cancel` | 撤销未完成的交接 |
| `POST /scans/upload` | 终端批量回传扫码事件，返回 accepted/replayed/forked 分类 |
| `GET  /plaques/position?plaque_id=` | 标牌当前应在位置、是否换位过、异常起点 |
| `GET  /deployments?deployment_id=` | 查看布设窗口关联的专区、展项与内容摘要 |
| `GET  /inspection-queue?site_id=&kind=&status=` | 巡检队列（默认只列未结工单） |
| `POST /inspection-cases/claim` | 领取工单，返回 `claim_expires_at` |
| `POST /inspection-cases/resolve` | 引用三类证据之一结案 |
| `GET  /inspection-cases/supplements?case_id=` | 查看重复/迟到事件的补充记录 |
| `GET  /statistics/anonymous?site_id=&day=` | 当日匿名统计口径与未结工单数 |

### 回传事件格式

```json
{
  "device_serial": "终端硬件序号（不入库）",
  "site_id": "site-001",
  "request_id": "可选的批量幂等键",
  "events": [
    {
      "plaque_code": "plaque-001",
      "event_seq": 0,
      "scanned_at": "2026-09-25T09:00:00Z",
      "zone_id": "终端上报位置（可选）",
      "exhibit_id": "终端上报展项（可选）",
      "content_digest": "终端读到的内容哈希（可选）"
    }
  ]
}
```

终端至少回传标牌身份、设备内单调序号和扫描时间；位置与摘要字段携带时才参与对应异常判定。
