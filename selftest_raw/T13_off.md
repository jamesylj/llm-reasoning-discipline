不能沿用上面的结论。两段早前讨论讲的是 **HTTP 客户端超时 + 重试放大**，和 **MySQL 连接池 wait_timeout** 是不同层面的问题，结论不能直接迁移。

## 建议值

**MySQL 连接池的 `wait_timeout` 建议设为 300 秒（5 分钟）左右**，并配合连接池的 idle 检测/保活机制。具体要按你的部署环境调整：

| 场景 | 建议 wait_timeout |
|---|---|
| 应用与 MySQL 同机房、连接池有保活 | 300–600s |
| 跨机房/云上、中间有 LB/NAT/防火墙 | 60–300s，且必须小于中间设备 idle 超时 |
| Serverless / 短连接为主 | 30–120s |
| 长事务、批处理连接 | 可放宽到 600–1800s，但要单独隔离连接池 |

## 依据

1. **`wait_timeout` 是 MySQL 服务端参数**，控制的是「非交互连接空闲多久后被服务端主动关闭」。它和 HTTP 客户端超时不是一回事：前者是 DB 侧回收空闲连接，后者是调用方等待响应的上限。

2. **真正的风险是「连接被中间层悄悄掐断」**。如果 NAT/LB/防火墙的空闲超时（常见 60s、300s、350s）小于 MySQL 的 `wait_timeout`，连接会在池子里看起来还活着、实际已被断开，取出来用时报 `Communications link failure` / `MySQL server has gone away`。所以经验法则是：
   - `wait_timeout` 要 **小于** 中间设备 idle 超时；
   - 连接池的 `maxLifetime` 要 **小于** `wait_timeout`；
   - 连接池的 `idleTimeout` / `validationInterval` 要能定期探活。

3. **设太短的代价**：频繁建连，握手 + 认证开销上升，QPS 高时反而制造延迟尖刺——注意，这里的「尖刺」成因和早前 B 说的「超时导致重试放大」**机制不同**，只是现象名字像，不能拿旧结论套。

4. **设太长的代价**：空闲连接长期占着 MySQL 的 `max_connections` 配额，浪费内存；且一旦中间层先断，坏连接滞留更久。

## 和早前结论的关系

- 早前 A/B 的结论（超时 + 重试放大）适用于 **HTTP 调用链**，不能直接搬到 DB 连接池。
- 唯一可迁移的**思路**是：都要关注「超时值之间的相对大小关系」以及「失败后的行为是否会放大问题」。但具体参数、层级、失效模式都不同，必须重新按 DB 侧的超时链条来定：
  `连接池 maxLifetime < MySQL wait_timeout < 中间设备 idle timeout`。

## 落地建议

- 先查清链路上所有 idle 超时：MySQL `wait_timeout`、LB/NAT、连接池配置。
- 用 `SHOW VARIABLES LIKE 'wait_timeout'` 看当前值，别只改应用侧。
- 连接池开启保活（如 HikariCP 的 `keepaliveTime`、`maxLifetime`），并确保 `maxLifetime` 比 `wait_timeout` 小几秒到几十秒。
- 上线后观察 `Aborted_clients`、`MySQL server has gone away` 类错误，再微调。

一句话：**300s 是常见起点，但真正决定值的是「链路上最短的那个 idle 超时」，而不是早前 HTTP 超时/重试的结论。**