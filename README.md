# 实现绿电收益与补偿联合分摊基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配和调度情景；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `src/revenue_settlement/`：多来源结算依据登记、冻结计量分摊、额度预占核销、撤销结转、人工改账复核与角色化收益视图；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

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
PYTHONPATH=src python3 -m wind_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
PYTHONPATH=src python3 -m revenue_settlement.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、健康测点分析、并网审批，以及统一收益归集（来源登记→冻结分摊→核销/撤销/结转→改账复核），不访问外部网络。

## 统一收益归集

`src/revenue_settlement/` 把同一发电周期的多张结算依据收敛为单一归集过程，避免同一兆瓦时被重复计入：

1. **登记来源批次**：市场电费（按时段 PEAK/FLAT/VALLEY 计价）、保障性收购、绿证、调峰奖励、限电补偿，逐批次登记适用机组范围、有效期、额度上限与使用顺序（`POST /sources`）。
2. **冻结计量并分摊预占**：对冻结的计量版本按使用顺序匹配首个可承接来源并预占额度；限电损失电量在确认损失前不参与分摊。结果由"计量行+来源规则+额度余额"哈希确定，重复分摊回放原批次（`POST /apportion`）。
3. **计量确认核销**：按实际电量核销预占，实际少于预占的尾差释放回来源额度，超出冻结电量的部分标记未分配（`POST /metering/versions/{id}/confirm`）。
4. **撤销 / 部分确认 / 执行失败**：撤销整批释放；执行失败把未核销预占结转到尚未确认的目标周期，无来源承接部分释放；已确认周期的规则变更不影响历史结果。
5. **人工改账**：经营申请、财务（非申请人）复核后生成新版本批次，全程进入审计哈希链。
6. **幂等与防重**：重复核销/结转请求返回原结果；复用编号提交不同内容返回冲突；同一版本不允许并存两个待核销批次。
7. **角色化视图**：场站视图隐藏单价、金额且仅限本场站；经营视图全量；财务聚焦核销与额度；审计只读全量并可校验哈希链。`GET /lines/{id}/explanation` 可解释一笔电量为何落到某个来源（含每个被跳过来源的具体理由）。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m revenue_settlement.api --database revenue.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON（请求头 `X-Actor-Id` 标识操作人）。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
