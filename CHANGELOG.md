# Changelog

## 0.2.0 - 2025-12-19
- Added validation mode using `subscriptionsv2.get` with eligible-for-revoke output.
- Added revoke-prorated mode and checkpoint/resume support.
- Added timestamped logs and optional response logging.
- Introduced folder structure (`configs/`, `inputs/`, `outputs/`, `logs/`, `checkpoints/`, `scripts/`, `secrets/`).
- Added test/prod config templates for validate and revoke.

## 0.2.1 - 2025-12-19
- Added revoke guard requiring validation output (`subscription_state` column).
- Added separate success/failed checkpoints and expanded report CSV fields.

## 0.1.0 - 2025-12-08
- Initial bulk cancellation script with logging and retries.
