按顺序推理如下：

1. **服务拓扑：Nginx → app（4 实例）→ Redis → MySQL**  
   这说明请求链路是：先到 Nginx，再进入 4 个 app 实例，然后访问 Redis，最后访问 MySQL。  
   因此，延迟可能来自 Nginx、app、Redis、MySQL 中任意一段，但后面要结合各段指标判断。

2. **app 的 p99 延迟为 2300ms**  
   这是端到端里 app 这一层的 p99 延迟，已经很高。  
   依赖第 1 步：app 位于 Redis 和 MySQL 之前，所以 app 的 2300ms 很可能包含了它调用 Redis 和 MySQL 的等待时间，而不一定是 app 自身 CPU 计算慢。

3. **缓存命中率 98%**  
   依赖第 1、2 步：app 会先查 Redis。98% 命中率说明绝大多数请求不需要回源 MySQL。  
   因此，正常情况下 Redis 应该能挡住大部分数据库压力。  
   但仍有 2% 未命中，会穿透到 MySQL。这个数字本身不能解释 2300ms，因为命中率很高。

4. **MySQL 连接数已达 max_connections，等待队列增长**  
   依赖第 1、2、3 步：虽然缓存命中率 98%，但仍有 2% 请求访问 MySQL；同时 app 有 4 个实例，每个实例可能维护连接池。  
   当 MySQL 连接数达到 max_connections 且等待队列增长时，说明 app 访问 MySQL 的请求会排队等待连接。  
   这会直接拉高 app 的响应时间，因为 app 在等 MySQL 连接时被阻塞。  
   所以第 2 步的 2300ms 很可能主要来自这里。

5. **MySQL 实例 CPU 利用率 15%**  
   依赖第 4 步：如果 MySQL CPU 只有 15%，说明数据库并不是因为 CPU 算不过来而慢。  
   结合第 4 步，问题更像是连接数打满导致排队，而不是 CPU 瓶颈。  
   也就是说，MySQL 可能“有能力处理”，但连接资源被占满，新请求进不来。

6. **Redis 的 P99 命令耗时 0.4ms**  
   依赖第 1、2、3 步：Redis 在链路中位于 app 和 MySQL 之间，且缓存命中率 98%。  
   Redis P99 只有 0.4ms，说明 Redis 本身非常快，不是延迟来源。  
   因此第 2 步的 2300ms 不能归因于 Redis。

7. **结论：延迟的主要来源是什么？**  
   依赖前面所有步骤：  
   - 第 1 步确定链路包含 MySQL。  
   - 第 2 步 app p99 高达 2300ms。  
   - 第 3 步缓存命中率 98%，说明大部分请求不走 MySQL，但仍有 2% 会走。  
   - 第 4 步 MySQL 连接数达 max_connections 且等待队列增长，说明访问 MySQL 的请求在排队。  
   - 第 5 步 MySQL CPU 仅 15%，排除 CPU 瓶颈。  
   - 第 6 步 Redis P99 仅 0.4ms，排除 Redis 瓶颈。  

   因此，**延迟的主要来源是 MySQL 连接池/连接数瓶颈导致的排队等待**，而不是 Redis、MySQL CPU 或 app 自身计算。  
   更具体地说：app 在访问 MySQL 时，由于 MySQL 连接数达到 max_connections，连接获取排队，导致 app 请求被阻塞，最终表现为 app p99 2300ms。  
   虽然缓存命中率 98%，但剩余 2% 的未命中请求，或连接池配置/长事务/连接泄漏等问题，足以把 MySQL 连接打满，从而拖慢整体 p99。