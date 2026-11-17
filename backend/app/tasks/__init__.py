"""异步任务层（P1.3）：Postgres 队列 + 后台 worker。

三个真实慢任务的后台化：简历 OCR 解析（1-2 分钟）、批量匹配重建（force 全量重算）、
定时主动发现。刻意不引入 Kafka/RabbitMQ：单机单用户下没有吞吐对象，
`FOR UPDATE SKIP LOCKED` 的行级锁足以支撑未来多 worker 横向扩容。

模块划分
    queue.py    队列原语：入队 / 领取（SKIP LOCKED）/ 完成 / 失败重试 / 取消 / 进度
    handlers.py 任务注册表：type → async handler(task, progress)
    runner.py   worker 循环：lifespan 内启动；启动时恢复上个进程遗留的 running 任务
"""

from . import handlers, queue, runner

__all__ = ["handlers", "queue", "runner"]
