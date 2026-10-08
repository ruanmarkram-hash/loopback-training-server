# Web QA

Run only with the resource slot assigned by the project owner. Uses the real isolated backend at `LOOPBACK_QA_ORIGIN` (default loopback origin). One browser worker, retries disabled. Synthetic credentials are read from the project private baseline env file; no traces/videos or automatic screenshots capture credentials. Explicit screenshots are taken only after login or on blank login forms. Test assertions use UI and authenticated public API, never private database access. Result evidence lives in project artifacts/web-qa.
