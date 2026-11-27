# 多对象容量及地址计算

执行 `python3 scripts/check_capacity.py --snapshot <文件>`（路径相对Skill）。输入由Agent
根据本轮证据整理，不要求用户填写中间结构。脚本不查云、不读凭据。

旧版假设算术输入（仅unbound_arithmetic_only，不证明实际容量）：
```json
{"operation":"capacity","items":[{"scope":{"quota":"route-entry","object":"vtb-example","direction":"all","family":"all"},"usage_scope":{"quota":"route-entry","object":"vtb-example","direction":"all","family":"all"},"basis":"actual","complete":true,"evidence":["原始配额和条目响应引用"],"limit":200,"used":3,"growth":198}]}
```

scope必须包含适用的账号/地域/对象/方向/地址族等维度。usage_scope必须一致；basis必须actual，
用量完整且有证据才输出fits/exceeds，否则unknown。数字必须非负整数；缺失不当0，负余量保留。
新增对象的used=0只有在明确计划中新建对象时成立。多条约束各自计算，任意约束超额即不能
按当前计划落地；存在unknown不能宣称整体可行。脚本不验证证据真实性或产品字段含义。

地址输入：
```json
{"operation":"addresses","parent":"10.0.0.0/16","cidrs":["10.0.4.0/24","10.0.6.0/22"]}
```

检查严格网络边界、父子包含、同族与重叠；不自动归一化主机位错误，以免掩盖与现有子网重叠。
输出addresses为数学总地址数，不减任何猜测的保留IP；可分配容量以产品返回和已核实规则为准。
重复CIDR也视为重叠。IPv6直接使用整数计算，不枚举地址。退出0表示检查已完成，仍须读valid/status；
输入结构错误退出1。

## 原始配额绑定与答案检查（首选）

`python3 scripts/check_capacity.py --snapshot input.json --evidence evidence.json`
先读 [证据契约](evidence.md)。`capacity_evidence` 输入示意：

```json
{
  "operation":"capacity_evidence",
  "context":{"account":"<account-id>","region":"<region>"},
  "window":{"start":"2026-01-01T10:00:00+08:00","end":"2026-01-01T10:00:30+08:00"},
  "items":[{
    "id":"vpc-region", "quota_ref":{"source":"capture-a","pointer":"/Result/Quotas/0"},
    "quota_identity":{"ProviderCode":"vpc","QuotaCode":"vpc","Dimensions":[{"Name":"region_code","Value":"<region>"}]},
    "usage_mode":"quota_total", "growth":2
  }],
  "answer_claims":[{"field":"/items/0/remaining","value":12}]
}
```

示例数字只是草稿待核对值，不是配额事实。quota_identity必须逐字段匹配选中原对象，Dimensions地域和Trn中的账号/地域/配额身份必须匹配context；输出
limit、used、remaining、remaining_after、utilization_percent及applicable_value_raw均从该对象生成。
未知/零上限使用率为null，不把除零当0%。重复配额/对象行不能用于并列两个互斥“实际”口径。
`answer_table`可直接引用；`answer_check`只验证显式声明字段，未知路径或类型不符均mismatch。
CLI退出0只表示运行成功，仍须检查items及answer_check。

单对象已有明确API配额计数时可用 `usage_mode:object_counter`，同时提供object_scope（对象ID）、
usage_object_ref（指向原对象）、object_id_pointer、counter_pointer（均相对对象），以及
accounting_verified:true和accounting_basis（实际读过的产品计数语义依据）。该模式只校验对象与
计数字段绑定，结果始终conditional、overall为unknown：不证明该配额适用于此对象/方向/地址族，
也不自动证明默认规则是否占额；accounting_basis不能是“猜测”。若API没有可靠计数，
或只有待解释的规则列表，用旧算术模式展示假设，结论保持未知，不能制造一个原API没有的counter。

VPC项目数量继续用summarize_vpc_quota.py的原始分页核对，并可通过
`--answer-claims claims.json`对其输出路径校验，例如/project_inventory/visible、/quota/remaining、
/quota/applicable_value_raw；不一致返回退出2。项目清单只展示project_inventory.vpc_ids对应项，
不要把全账号可见清单作为项目明细。共享/自有/未知归属分别展示，不把列表长度覆盖配额用量。
