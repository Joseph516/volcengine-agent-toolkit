# 保存证据与引用

使用新证据模式时，在任务工作目录分别保存 `evidence.json`（采集记录）和 `input.json`（计算计划）。
不要为了通过校验重写采集记录、删除不利字段或将当前时间改为历史时间。
每个来源以唯一名称索引，原始 `request` 和 `response` 保留完整；脱敏只能处理秘密值。

```json
{
  "capture-a": {
    "kind": "current_configuration",
    "observed_at": "2026-01-01T10:00:01+08:00",
    "context": {"account": "<account-id>", "region": "<region>"},
    "request": {"Action": "<actual-action>", "Region": "<region>"},
    "response": {"ResponseMetadata": {"RequestId": "<request-id>"}, "Result": {}}
  }
}
```

引用形如 `{"source":"capture-a","pointer":"/Result/Quotas/0"}`，pointer为JSON Pointer，
只定位原返回的对象/字段，不允许把摘录改造成原始响应。顶层计算输入含同样的context和
`window:{"start":"2026-01-01T10:00:00+08:00","end":"2026-01-01T10:00:30+08:00"}`。
合法的同轮查询窗口允许多次请求有不同时间，不要求时间字符串完全相同。

历史配置证据使用 `kind:historical_configuration`，另存 `valid_from_pointer`/`valid_to_pointer`，定位原响应中实际
配置版本/审计证据的有效区间字段，区间必须覆盖目标窗口；保留采集时间和原记录。当前对象的UpdateTime、
日志REJECT或NODATA不证明历史配置有效区间。没有这样的证据就报告未知，不能自行补区间。

脚本核对来源的账号/地域、请求和成功回执、时间覆盖、JSON路径及关联关系，输出每个使用来源的
SHA256与RequestId用于复查。**哈希不是签名，不证明采集真实性；本地文件可被修改。**
规则优先级、分页过滤语义、历史区间出处仍须审查原始采集流程，不能声称校验器防止了一切伪造。
来源缺失、null或失败不得转换成空列表/零。
