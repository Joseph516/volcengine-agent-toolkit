# 配额取数与统计口径

## 权威入口

- [VPC 约束限制](https://www.volcengine.com/docs/6401/67810)：用于确认默认能力和计数规则；账号实际限额仍需实时查询。
- [DescribeVpcs](https://api.volcengine.com/api-docs/view?serviceCode=vpc&version=2020-04-01&action=DescribeVpcs)：`AccountId` 为资源归属账号，`ProjectName` 为项目，`NextToken` 为空表示结束。
- [ListProductQuotas](https://api.volcengine.com/api-docs/view?serviceCode=quota&version=2022-07-01&action=ListProductQuotas)：配额项、维度、用量和可调性。
- [ListProductQuotaDimensions](https://api.volcengine.com/api-docs/view?serviceCode=quota&version=2022-07-01&action=ListProductQuotaDimensions)：产品支持的配额维度。

引用前读取与当前问题相关的正文或 CLI 详细帮助。链接不可读时说明来源缺口，
不要沿用别家云厂商的同名接口定义。下述命令是入口，实际参数及分页能力以当前帮助为准。
所有 `ve` 调用（包括帮助）使用 SKILL.md 中的调用方标识与已确认身份。

```bash
ve quota ListProducts --help --detail
ve quota ListProductQuotaDimensions --help --detail
ve quota ListProductQuotas --help --detail
ve vpc DescribeVpcs --help --detail
```

已确认产品代码和地域维度后，构造 JSON 参数再传入 `--body`，避免 shell 解释 JMESPath
反引号。例如产品返回的代码为 `vpc` 且维度为 `region_code` 时，使用该精确值；
若返回为不同大小写则沿用返回值，不能凭示例强制改写。

```bash
# region/provider_code 均来自本轮已确认的范围与产品返回，不是固定账号值。
quota_body=$(python3 - "$region" "$provider_code" <<'PY'
import json, sys
print(json.dumps({"ProviderCode": sys.argv[2], "MaxResults": 100,
                  "Dimensions": [{"Name": "region_code", "Value": sys.argv[1]}]}))
PY
)
ve quota ListProductQuotas --region "$region" --body "$quota_body" --output json
ve vpc DescribeVpcs --region "$region" --MaxResults 100 --output json
```

保留每页结果及实际请求参数。按返回 NextToken 继续，用同样的过滤器；不能把成功的
第一页当作全量。空列表、403、超时和未查到后续页分别记录。若使用 PageNumber/PageSize，
必须完整读到 TotalCount，并检查跨页重复 ID/总数变化；离线辅助脚本仅接受 token 分页。

## 项目数量与账号配额

“项目下多少个 VPC”与“还能创建多少个 VPC”使用不同维度：

1. 项目数量：用项目过滤或完整清单中的 `ProjectName` 匹配；同时保留所有权信息。
   若同项目名的其他账号资源也可见，分别报告本账号自有数和其他账号可见数；不把它们
   合成“本账号项目拥有数”。缺 `AccountId` 时归属未知，不从名称或标签猜所有者。
2. 账号地域配额：选择精确 `QuotaCode=vpc` 和目标维度的记录，使用它自己的
   `TotalQuota` 和 `TotalUsage.Value`；不要误选 `vpc/subnet` 或 `vpc/route_table`。
3. 对比：区分项目自有、地域自有、其他账号可见和未知归属。证明其他账号所有权只解释
   该部分计数，不自动证明共享方式、所有差异原因或查询已覆盖账号所有资源。

`TotalUsage` 缺失或未知不能当 0。即便清单完整，也应把清单推算与配额中心余量分列；
权限过滤、共享视图、状态计入规则及采集时点都可能影响对比。无证据时原因保持未知。

## 单对象容量

先读每项配额的 `Description/Dimensions`，再决定分组键：

- 路由表数量：按 VPC；配额注明系统+自定义时两者都计入。类型需要路由表自身字段，
  不能从关联子网的类型推断另一张未关联表。
- 自定义路由条目：按每张路由表计数；当限制明确仅适用于自定义项时排除系统项。
  下一跳类型与条目类型不是同一字段，不仅凭 CIDR 字符串猜类型。
- 安全组/ACL 条目：按对象、方向、地址族。方向未知的增量可以展示每种分配的条件结果；
  “每方向各加 N 条”与“合计加 N 条”不可互换。
- 网卡数：规格限额、实例已挂主/辅网卡和账号地域辅助 ENI 总量分别核对；
  有剩余挂载槽位仍要检查目标网卡地域/可用区/VPC/状态，不能据此宣称挂载成功。
- 流日志/镜像：区域总量不能替代每源/目的/会话关联上限；用实际关联关系计算每个对象，
  不把尚未创建的对象说成已具备采集或连通能力。

可调范围必须与同一条配额记录一起读取。不要仅凭 `continuous` 编造区间；
仅有默认文档时，明确条件计算，不将历史账号快照或示例数字固化到 Skill。
