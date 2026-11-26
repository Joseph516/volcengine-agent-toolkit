# 多对象容量及地址计算

执行 `python3 scripts/check_capacity.py --snapshot <文件>`（路径相对Skill）。输入由Agent
根据本轮证据整理，不要求用户填写中间结构。脚本不查云、不读凭据。

数量输入：
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
