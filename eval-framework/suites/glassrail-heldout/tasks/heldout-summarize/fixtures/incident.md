# Payment API Incident Review

**Owner:** Mara Iqbal
**Date:** 2025-11-04

## Summary

The payment API experienced elevated failures during a regional deploy. Error
rate reached **18%** for 27 minutes before rollback completed. The incident did
not affect stored card data, and no duplicate charges were confirmed.

## Follow-up

Mara Iqbal will add regional canary checks before the next deploy. The team will
also lower the rollback threshold and rehearse the paging path in December 2025.

The incident was unrelated to the earlier search-index outage, which had a 9%
timeout rate.
