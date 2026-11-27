# 离线检查输入

在Skill目录执行 `python3 scripts/check_network.py --snapshot <文件>`。仅Python标准库，
不联网、不读取环境变量、不会修改文件。退出0表示计算成功，必须阅读业务status；输入错误退出1。
原始API字段映射由Agent按实际帮助/返回完成，保留来源于任务工作目录。下面是合成格式，不是API请求。

## scope（仅假设集合计算）：同一协议/端口下的源和目的集合

```json
{"operation":"scope","allowed":{"sources":["192.0.2.1/32"],"destinations":["198.51.100.1/32"]},"proposed":{"sources":["192.0.2.1/32"],"destinations":["198.51.100.0/24"]}}
```

输出extra为允许范围之外的CIDR集合。按有效允许集合整理，不能把原始单层规则直接输入。
每次只检查一个固定协议、端口和源/目的笛卡尔积；多条配对关系必须分流检查，不能把多个
独立配对混成两份集合而引入交叉访问。此检查不证明期望流量全部被允许，也不验证端口/优先级。

## route：最长前缀

```json
{"operation":"route","destination":"192.0.2.10","complete":true,"associated":true,"evidence":["保存的响应引用"],"routes":[{"cidr":"192.0.0.0/16","next_hop":"eni-example-a"},{"cidr":"192.0.2.0/24","next_hop":"eni-example-b"}]}
```

没有完整列表/证据为unknown，同最长前缀多条为ambiguous，不猜ECMP或优先级。
associated只代表已核实关联，本检查不证明下一跳健康或可达。CIDR必须合法网络边界。

## path（仅假设关卡计算）：同时间快照关卡合取

顶层为 `operation: "path"`、`target_time: "实际查询窗口标识"`、`gates: {...}`。
关卡名：source_sg_egress、destination_sg_ingress、source_acl_egress、destination_acl_ingress、
destination_acl_egress_return、source_acl_ingress_return、forward_route、return_route。
每个关卡形如：

```json
{"decision":"allow","applicable":true,"complete":true,"observed_at":"实际查询窗口标识","evidence":["原响应/字段/规则ID引用"]}
```

decision为allow/deny/unknown。applicable表示已确认绑定/适用，complete表示已完成该关卡
所需的全量策略和优先级计算；不要把未绑定ACL作为deny。确认没有绑定ACL可依据关联查询
记录allow，不能把没有查询结果当allow。历史与当前观察混用为unknown。
任意已证实deny得到configuration=blocked，并同时列未知关卡；所有关卡allow才为allows。
数据面始终not_verified：这不是端点探测，不据此给唯一根因或恢复承诺。

## sg_scope：绑定完整网卡清单的共享SG影响范围

先读 [证据契约](evidence.md)，执行
`python3 scripts/check_network.py --snapshot input.json --evidence evidence.json`。
顶层context/window与证据契约一致，其他字段：

```json
{
  "operation":"sg_scope", "security_group_id":"sg-example", "family":4,
  "inventory_action":"DescribeNetworkInterfaces",
  "eni_pages":[{"items_ref":{"source":"eni-page-1","pointer":"/Result/NetworkInterfaceSets"},"total_ref":{"source":"eni-page-1","pointer":"/Result/TotalCount"}}],
  "fields":{"id":"/NetworkInterfaceId","security_groups":"/SecurityGroupIds","addresses":[{"pointer":"/PrivateIpSets","value_pointer":"/PrivateIpAddress"}]},
  "rule_sources":["192.0.2.1/32"],
  "allowed":{"sources":["192.0.2.1/32"],"destinations":["198.51.100.1/32"]}
}
```

字段路径按真实返回核实，不假定示例字段一定存在。当前适配器支持SG ID字符串数组、IP字符串数组（value_pointer为空）及嵌套IP对象数组。
fields.addresses列出目标地址族所有主/辅助地址数组及其值路径；没有TotalCount或只有token分页时保持unknown，补充明确适配器后再用，
不要改写API原文伪装支持。每页来源保存的request含Action/Region及实际分页参数，拒绝其他过滤项，
防止只查询目标ECS/已挂载网卡而遗漏共享成员；不得超出用户授权去查询。
脚本核对稳定总量和唯一ENI数，从全部成员地址派生destinations，忽略调用方自填的proposed。
输出是新增SG入向规则的**潜在影响面**，不证明每条路径实际可达；既有策略可能扩大范围，
minimum_change_verified始终false。支持一个固定协议/端口下的源、目的集合，出向及多流组合不适用。

## path_evidence：绑定目标关联与时间的关卡

顶层operation为path_evidence，含context/window、endpoints和gates。
endpoints固定源/目的IP和其原始ENI对象，例如source（destination同结构）：

```json
{"source":{"ref":{"source":"eni-a","pointer":"/Result/Eni"},"id_pointer":"/Id","subnet_pointer":"/SubnetId","addresses_pointer":"/PrivateIpSets","address_value_pointer":"/PrivateIpAddress","ip":"192.0.2.1"}}
```

脚本核对IP确实在该网卡，源/目的SG关卡必须绑定对应ENI，ACL关卡绑定对应子网，
forward_route绑定源子网，return_route绑定目的子网。不能拿源SG替代全部八个关卡。
关卡名称与旧path相同，每个关卡：

```json
{
  "target_id":"eni-example",
  "target_ref":{"source":"eni-a","pointer":"/Result/Eni/Id"},
  "association_ref":{"source":"eni-a","pointer":"/Result/Eni/SecurityGroupIds"},
  "policies":[{"id":"sg-example","ref":{"source":"sg-a","pointer":"/Result/SecurityGroup"},"id_pointer":"/Id","rules_pointer":"/Rules","rules_total_pointer":"/TotalCount"}],
  "decision":"allow", "complete":true,
  "semantics_verified":true, "decision_basis":"实际规则、优先级与多SG聚合依据"
}
```

这是归一化的引用格式，不是云API定义；路径必须指向实际原文，target_ref和association_ref必须
是同一来源同一资源对象下的字段。policies必须覆盖关联数组中的全部ID，策略对象ID必须一致；ACL应引用
实际子网关联对象，不能引用另一个同VPC的ACL。确认无ACL可传policies:[]和decision:allow，
但关联原值必须是[]；null、缺字段不算空。多SG须先完整取证并在decision_basis记录全部策略聚合，
脚本校验全部关联策略均被引用，但不声称已经实现完整防火墙模拟。未支持的API形状保持未知。

observed_at不再接受调用方关卡上的覆盖值；实际窗口由独立采集记录检查。当前快照即使UpdateTime
很早，也不能证明历史绑定或规则；历史有效区间必须有版本/审计依据。任何已绑定deny会保留其他
unknown，不能把“某层配置阻断”扩写为唯一历史根因。runtime_reachability与unique_root_cause
始终not_verified。

策略还必须引用非null规则数组rules_pointer，并以rules_total_pointer核对原始总数。
SG/ACL属性接口返回的内联完整规则数组可用rules_mode:inline_attributes替代总数：
仅接受对应DescribeSecurityGroupAttributes/DescribeNetworkAclAttributes且原请求无分页参数。
这不支持将某一分页、单条规则或投影null冒充全量。接口形状不支持时报告未知，不制造计数。
