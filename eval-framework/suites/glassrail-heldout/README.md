# glassrail-heldout

Held-out confirmation suite for Glassrail.

This suite estimates generalisation. It is run only at gate/confirmation time,
and its failures must not be used to tune engine prompts, cookbook keywords, or
heuristics. If a task needs to be studied to debug a failure, move that task
into the main `glassrail` suite and replace it here with a new held-out task.

Publish this suite's numbers beside the main suite numbers. A widening gap
between main and held-out results is an overfitting signal.
