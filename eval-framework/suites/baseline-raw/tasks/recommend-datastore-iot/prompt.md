I'm building an IoT platform that ingests sensor readings from 50,000 devices
at one reading per second each. Queries are almost always time-range aggregations
(e.g. average temperature over the last hour). Writes vastly outnumber reads and
the data is always time-ordered. Compare a general-purpose relational database,
a document store, and a time-series database for this workload, then recommend
the best fit.
