# 离线检查输入

在Skill目录执行 `python3 scripts/check_network.py --snapshot <文件>`。仅Python标准库，
不联网、不读取环境变量、不会修改文件。退出0表示计算成功，必须阅读业务status；输入错误退出1。
原始API字段映射由Agent按实际帮助/返回完成，保留来源于任务工作目录。下面是合成格式，不是API请求。

## scope：同一协议/端口下的源和目的集合

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

## path：同时间快照关卡合取

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
