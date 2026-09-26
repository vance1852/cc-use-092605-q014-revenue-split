# 实现绿电收益与补偿联合分摊基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配和调度情景；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `src/revenue_settlement/`：绿电收益与补偿联合归集（来源批次登记、冻结计量分摊与额度预占、确认核销、撤销/结转、人工改账复核、场站/经营/审计三视图与收益溯源）；
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

命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、健康测点分析、并网审批，以及同一发电周期内市场电费、保障性收购、绿证、调峰奖励与限电补偿的联合归集，不访问外部网络。

### 收益归集过程

1. **登记来源批次**：经营登记市场电费（按时段计价）、保障性收购（额度上限）、绿证与调峰奖励（适用机组）、限电补偿（按确认损失电量），记录适用范围、有效期、上限与使用顺序；规则修订走乐观锁新版本，旧分摊运行继续绑定旧快照。
2. **冻结计量并预占**：场站冻结计量版本后，经营针对该版本计算分摊；市场电费与保障性收购共享上网电量池并按顺序占用（同一 MWh 不重复计列），绿证/调峰按适用机组的全部计量电量独立计列，限电补偿在冻结阶段不计列；各来源额度同步预占。
3. **确认核销**：计量确认时按实际电量核销，部分确认差额释放回额度池；已确认损失电量此时才产生限电补偿。撤销预占释放额度；执行失败不释放而结转到后续周期。已确认周期不再受后续规则变更影响。
4. **人工改账**：场站发起、必须由不同的经营人员复核，批准后生成分摊行新版本并留下完整事件链。
5. **幂等与隔离**：核销、改账通过幂等编号防重——重复请求返回原结果，复用编号提交不同内容一律拒绝。场站视图隐藏价格与金额并按场站隔离，经营视图掌握额度余额，审计视图留存改账与版本历史；`GET /applications/{id}` 可解释每笔收益落到某来源的原因。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m revenue_settlement.api --database revenue.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
