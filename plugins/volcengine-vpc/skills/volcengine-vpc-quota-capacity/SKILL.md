---
name: volcengine-vpc-quota-capacity
description: >-
  Check Volcengine VPC quotas, scoped resource usage, remaining capacity and proposed growth.
  Use for VPC/subnet counts, route-table or route-entry limits, security-group/network-ACL
  rule capacity, ENI/flow-log/traffic-mirror capacity, and read-only quota reviews.
  核对火山引擎 VPC 配额、项目资源数量、剩余额度、历史容量变化及新增资源是否超限；区分账号地域、
  项目和单对象口径。用于配额与容量核对，不用于执行资源变更或提交提额申请。
license: MIT
metadata:
  openclaw:
    requires:
      bins: [ve, python3]
---

# VPC 配额与容量核对

给出**同一统计口径下**的实际配额、当前用量、计划增量与剩余额度，并标明每个数字的来源。
本工作流只读；“余量足够”不代表已经创建成功、资源库存充足或获准执行变更。

## 确定范围与身份

- 从请求和已有上下文取得地域、项目、目标对象与计划增量。项目用于筛选业务资源，
  不自动成为配额维度。未给项目时不要擅自限定为 `default`。
- 只澄清影响计算的缺失项。例如“加两条规则”缺方向/地址族时，可分别给条件计算，
  标明假设；不要替用户选择实际规则。新 VPC 的子网数从该新对象的计划计算。
- 按已安装的 `volcengine-cli` Skill 使用官方 `ve`。每次进程带上
  `VE_CALLER_TYPE=ai_agent`、实际宿主的 `VE_CALLER_NAME`，以及本 Skill 名称
  `VE_SKILL_NAME=volcengine-vpc-quota-capacity`。明确传入目标 `--region`。
- 若用户要求回放保存的快照，使用快照中的身份和时间，跳过实时 CLI/STS 核验；
  明确结果只代表快照时点。实时查询时，用 `ve version` 和 `ve sts GetCallerIdentity --region <region>` 核验工具与实际账号，
  与用户指定账号核对；只使用用户已选身份。遇到 403、过期或身份不符，报告具体失败动作，
  不改 IAM、不读取/打印凭据文件、不自行切换到其他账号或角色。

## 获取配额与用量

使用 CLI 帮助/官方 API 文档核对参数，优先复用本轮已取得且范围相同的完整返回。

1. 通过 `quota ListProducts` 确认产品代码，按返回值保留大小写；通过
   `ListProductQuotaDimensions` 确认适用维度，再调用 `ListProductQuotas` 或
   `GetProductQuota` 查询目标地域/对象的实际配额。完整读取分页，保存原始响应、
   请求参数、查询时间和 RequestId。不要用 `head` 截断结果后宣称已查全。
2. 将每一项配额按 **ProviderCode + QuotaCode + Dimensions** 单独匹配，保留它自己的
   `Description`、`TotalQuota`、`TotalUsage`、`ApplicableType`、`ApplicableValue`。
   可调性和范围不能从相邻项、文档默认上限或另一种资源复制。
3. 按下面的对象口径查询真实用量。账号地域配额用量与单对象用量分别展示；
   `TotalUsage` 未明确指向目标对象时，不能套给任意 VPC、路由表或安全组。
4. 若实时配额不可用，明确缺失原因；官方默认限制只能作为标注清楚的条件参考，
   不将其写成账号实际配额，不据此给无条件“可创建”结论。

| 核对对象 | 用量与容易混淆的口径 |
| --- | --- |
| 项目 VPC 数量 / 账号地域 VPC 配额 | 用 `DescribeVpcs` 完整分页及 `ProjectName` 定位项目；以返回 `AccountId` 区分自有、其他账号可见、归属未知。账号可见条数不等于本账号配额用量。 |
| 单 VPC 子网 | 按目标 `VpcId` 读取子网；不要用账号所有子网之和减单 VPC 上限。新建 VPC 的子网配额按新对象计划计算。 |
| 路由表 / 路由条目 | 分别查 `DescribeRouteTableList`、`DescribeRouteEntryList`；使用实际类型字段区分系统/自定义，按配额说明决定计入项。无子网关联不意味着自定义表或可清理。每张表单独计算。 |
| 安全组 / 网络 ACL 规则 | 读取对象属性中的完整规则；按入/出方向及 IPv4/IPv6 分桶。系统兜底条目是否占额须核实；不确定时报告条件范围，不能把规则数、优先级最大值和配额混用。 |
| ENI / 私网 IP | 用 ECS 实例属性和 `DescribeInstanceTypes` 的实际规格限制，结合已挂 ENI 与地址列表；核对主网卡/主 IP 是否计入。实例规格限制和地域 ENI 配额是两项独立约束。 |
| 流日志 / 流量镜像 | 地域总量与单 ENI、单会话、目的或筛选器的限制分开；对象用量来自对应列表及关联关系。配额有余量不等于该规格支持镜像或已有采集配置。 |

具体字段核对及 CLI 示例按需读 [references/accounting.md](references/accounting.md)。

## 计算与差异解释

- 配额口径明确时：`当前余量 = 实际上限 − 同口径用量`；
  `计划后余量 = 实际上限 − 同口径用量 − 计划增量`。
  分别标清当前和计划后数值；负值表示超额，不截成零；缺失值不按零处理。
- **VPC 账号地域剩余额度采用匹配配额项的 TotalQuota 与 TotalUsage.Value。**
  清单计数用于交叉核验，不直接覆盖配额用量。先检查地域、账号归属、过滤条件、分页和采集时间。
  即使数值不同，也不能无证据归因于“最终一致性延迟”。无法解释就并列数据、保留未知。
- VPC 计数可使用 [scripts/summarize_vpc_quota.py](scripts/summarize_vpc_quota.py)
  对本轮保存的原始返回做离线核对；格式见 [references/snapshot.md](references/snapshot.md)。
  它不访问云、不读取凭据，不处理其他配额项或单对象容量；不要把它的结果推广到那些对象。
- `ApplicableType` 表示可申请的方式，不是已获批。`ApplicableValue` 缺失、格式不明，
  或范围与当前特殊配额相矛盾时，只报告原值和待核实项，不承诺一定能调到某值。
- 巡检按用户阈值或已读取规则标记风险；没有阈值时报告用量/余量和数据缺口。
  不因为资源带测试标签、未绑定或没有告警规则就判定必须删除/整改。

历史对比先按对象ID及相同项目/VPC范围对齐，再比较同一字段。历史配额最大值未注明所属对象时，
不能与当前某个VPC用量相减，或断言该VPC新增了哪些资源。对象不再出现只表示该查询范围未观测到，
不直接称已删除、业务无影响或风险改善；改名按ID视为同一对象。查询参数不保证过滤成功，
须核对返回的ProjectName等字段；项目外对象不能混入项目清单。

## 离线计算校验

凡输出实际配额、用量、余量、使用率或可申请档位，都运行离线校验；单对象也不跳过。
VPC清单/项目计数用 `summarize_vpc_quota.py`；其他配额使用
[scripts/check_capacity.py](scripts/check_capacity.py) 的 `capacity_evidence` 模式，
从同一原始配额对象读取QuotaCode、Dimensions、TotalQuota、TotalUsage与ApplicableValue，
不要手工复制邻项数字。对象计数口径未定（如ACL默认规则是否占额）就保留未知/条件计算，
不允许靠填写actual、complete=true把两个互斥口径都变成事实。

保存证据方式见 [references/evidence.md](references/evidence.md)，计算与答案字段契约见
[references/calculations.md](references/calculations.md)。旧capacity模式仅作假设算术，
不能据其fits/exceeds声称原始证据已核实。CIDR仍用addresses模式。
历史对象清单/计数对比用 `compare_inventory`：分别引用两时点原始清单及窗口，输出对象ID、
范围和差值；缺历史对象级数据时报告不可比，不用配额最大值伪造对象计数。

最终明细直接复用脚本生成的 `answer_table` 或VPC汇总结构。草稿中所有业务数字
及资源ID（包括附带的可申请档位、使用率、项目数量）用 `answer_claims` 指向输出字段核对；
若有mismatch先修正草稿。未列入claims的文字不在程序校验范围内，不能宣称全文已验证。
使用率直接用生成百分比，不另写可能矛盾的“个位数”“接近耗尽”等摘要。

## 交付

先回答用户问的数量或容量结论，再列必要明细：对象/范围、实际上限、同口径用量、
当前余量、计划增量、计划后余量、可调性与来源。来源包含 API/字段、地域、查询时间及
RequestId，产品限制附实际读过的官方文档链接。

区分“已核验”“条件计算”“未知”。仅对本轮查过的对象声明覆盖；查询失败不等于不存在，
未查 CLB/EIP 等关联服务不能断言没有。输出候选措施即可，本 Skill 不提交提额、
创建/删除资源或修改路由/规则。
