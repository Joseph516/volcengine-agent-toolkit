# VPC 计数离线核对

`scripts/summarize_vpc_quota.py` 只对已保存的原始 API 响应计算，不联网，不读取凭据。
它支持账号地域的 `vpc` 配额及项目数量；单对象或其他产品配额走 SKILL.md 的对应流程。

```bash
python3 scripts/summarize_vpc_quota.py --snapshot /path/to/snapshot.json --planned-vpcs 1
```

路径相对本 Skill 根目录。输入由 Agent 从本轮实际请求/响应整理，不能要求用户手填中间数据，
不能虚构成功的响应、RequestId、采集时间或已取得完整分页。原始资料保存在评测/任务工作目录，
不要把真实账号、资源或凭据提交进 Skill 仓库。

## 文件结构

```text
{
  "schema_version": 1,
  "collected_at": "<带时区的实际采集时间>",
  "region": "<目标地域>",
  "project": "<项目名；不限项目则为 null>",
  "provider_code": "<ListProducts 返回的精确 VPC 产品代码>",
  "identity": <GetCallerIdentity 完整 JSON 响应>,
  "vpc_pages": [
    {"request": {"Region": "<地域>", "MaxResults": 100},
     "response": <DescribeVpcs 完整 JSON 响应>},
    {"request": {"Region": "<地域>", "MaxResults": 100, "NextToken": "<上一页 token>"},
     "response": <下一页完整 JSON 响应>}
  ],
  "quota_pages": [
    {"request": {"Region": "<CLI --region>", "ProviderCode": "<产品代码>",
                 "Dimensions": [{"Name": "region_code", "Value": "<地域>"}], "MaxResults": 100},
     "response": <ListProductQuotas 完整 JSON 响应>}
  ]
}
```

每个数组只放实际存在的页。`Region` 保存 CLI 的地域参数，其余为原请求参数；
配额请求可选 `QuotaCode: "vpc"`。保持同一已核验身份和采集窗口。
为核对账号清单，VPC 请求不能带项目/名称/ID/owner 等过滤；项目在完整响应上筛选。
若授权只允许项目查询，直接按该授权查询并手工报告范围，不为运行此脚本扩大取数范围。

每页必须含 `ResponseMetadata`（Action、Service、RequestId）及完整 `Result`，VPC 响应需
保留 Region。脚本检查请求 token 串联、末页空 token、已有 TotalCount、重复 VpcId、地域/
配额 TRN 账号、精确配额记录；缺页、API 错误、歧义、缺失用量或负数哨兵值不产生容量结论。
PageNumber/PageSize 形式暂不支持；不要篡改历史响应伪造 NextToken，可按原响应人工复核。

输出保留可见、自有、其他账号及归属未知的计数。`quota.remaining` 始终是配额字段相减，
不会用清单条数替换用量；`ApplicableValue` 只原样报告，不自动解释范围或承诺提额。
库存差异仍未知时报告 warnings；所有权缺失则状态 `partial`。

退出码：0 为结构/计算通过（仍需读取 warnings），2 为有未知所有权的部分结果，1 为证据错误。
输出明确是保存的快照，不能将历史回放称为刚完成的实时查询。脚本只能检查所提供结构的
自洽性，无法证明响应真实性、身份未中途切换、API 权限未过滤资源或当前云端状态未变化。

用量必须携带数量单位 `个`、`count` 或 `Count`；未知、缺失或非数量单位拒绝计算，原单位保留在输出中。
