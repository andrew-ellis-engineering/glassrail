# Meridian Infrastructure: Annual Systems Review 2023

**Prepared by:** Chief Architect Marcus Webb  
**Review Date:** 2023-11-30  
**Classification:** Internal

---

## Executive Summary

The 2023 review covers infrastructure changes across four divisions. Total
capital expenditure on infrastructure reached **$2.4 million**, a 15% increase
over 2022. The most significant outcome was a **67% reduction in P99 latency**
on the customer-facing API, achieved by migrating from a monolithic cache to a
distributed cache cluster in Q3. The migration was led by senior engineer
**Priya Nair** and completed two weeks ahead of schedule.

## Division Highlights

### Division A — Platform

Platform introduced a new deployment pipeline in February 2023. Deployment
frequency increased from 4 per month to 22 per month. Mean time to recovery
(MTTR) dropped from 47 minutes to 9 minutes. The on-call rotation was expanded
from 6 engineers to 11 engineers.

A planned migration of the legacy authentication service was deferred to Q1
2024 due to a vendor dependency on a deprecated TLS library. This is the
highest-priority carry-over item for next year.

### Division B — Data

Division B completed the data lake migration to object storage in April 2023.
Storage costs fell by **38%** compared to the previous SAN-based solution.
Data pipeline job success rate improved from 91.2% to 99.1%.

The division operates **14 petabytes** of raw data as of year-end. Three new
streaming pipelines were added, bringing the total to 27 active pipelines.

### Division C — Security

No critical vulnerabilities were disclosed in production systems in 2023.
Two medium-severity issues were patched within the 72-hour SLA. The penetration
test conducted in October 2023 by external firm Redwood Security found no
high-severity findings.

Security awareness training completion reached 97% across all staff, up from
84% in 2022.

### Division D — Network

Core network throughput capacity increased from 40 Gbps to 100 Gbps after an
inter-datacenter link upgrade in July 2023. Packet loss on the primary link
averaged **0.002%** over the year, below the 0.01% SLA target.

A secondary datacenter interconnect was provisioned as a standby link in
September 2023. Its activation is planned for Q2 2024 following load testing.

## Budget Summary

| Item | Budget | Actual | Variance |
|---|---|---|---|
| Server hardware | $800,000 | $762,000 | -$38,000 |
| Network upgrades | $450,000 | $498,000 | +$48,000 |
| Cloud services | $700,000 | $680,000 | -$20,000 |
| Security tooling | $250,000 | $241,000 | -$9,000 |
| Miscellaneous | $200,000 | $219,000 | +$19,000 |
| **Total** | **$2,400,000** | **$2,400,000** | **$0** |

The budget came in exactly on target at $2.4 million.

## Key Risks Heading into 2024

1. The deferred authentication service migration (Division A carry-over).
2. The secondary datacenter interconnect activation requires extended load
   testing — any delay pushes the planned Q2 2024 go-live.
3. Data lake capacity is projected to reach 20 petabytes by mid-2024; storage
   procurement lead times must be initiated in Q1.

## Acknowledgements

Marcus Webb thanks the infrastructure teams across all four divisions for their
contributions. Special recognition to Priya Nair for delivering the distributed
cache migration ahead of schedule, and to the Division B team for the data lake
migration's cost outcomes.
