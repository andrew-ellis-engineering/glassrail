I'm building an e-commerce order management system. Transactions must be
ACID-compliant. The workload is many small reads and writes (order creation,
status updates, inventory decrements). Queries often join orders, line items,
customers, and products together. Compare a time-series database, a document
store, and a relational database for this workload, then recommend the best fit.
