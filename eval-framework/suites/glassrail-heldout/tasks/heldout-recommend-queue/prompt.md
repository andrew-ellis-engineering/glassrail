I need to distribute retry-safe background jobs to many workers. Volume is high,
jobs can be processed asynchronously, and workers must be able to claim work
without stepping on each other. Compare a message queue with a shared database
table for this workload, then recommend one.
