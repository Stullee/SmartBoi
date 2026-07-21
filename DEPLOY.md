# Deploying on Home Assistant OS

This covers running SmartBoi as a Home Assistant add-on on a Home
Assistant OS (HAOS) host, alongside the existing TradingBot add-on.

SmartBoi needs **no broker connection at all** to do useful work: EDGAR
ingestion, news ingestion, and the dossier engine (relationship
extraction, dossier updates, the skeptic pass) only need `EDGAR_USER_AGENT`,
a Finnhub API key, and an Anthropic API key respectively -- see the main
`README.md`'s recommended setup order. IB Gateway is only needed for the
optional, read-only price feed that lets the paper journal actually open
and mark hypothetical positions (`ENABLE_IB_PRICE_FEED`).

## 1. (Optional) Reuse the existing IB Gateway

If you already run TradingBot's IB Gateway (`deploy/ib-gateway/` in the
[TradingBot repo](https://github.com/Stullee/TradingBot), per its own
`DEPLOY.md`), SmartBoi can point at the exact same instance -- it only
ever opens a second, read-only client connection (a different
`ib_client_id` than TradingBot's own, and different again from its report/
dashboard clients). No separate Gateway container needed.

If you don't already have one running and want the price feed eventually,
follow TradingBot's `DEPLOY.md` section 1 to set one up first.

## 2. Install the SmartBoi add-on

1. In Home Assistant: `Settings -> Add-ons -> Add-on Store -> ⋮ -> Repositories`,
   add `https://github.com/Stullee/SmartBoi`.
2. The "SmartBoi (Evidence Synthesis, Paper-Only)" add-on should appear --
   install it. The first install builds the Docker image (pulls the code
   from this repo's git history), which takes a couple of minutes.
3. Open the add-on's **Configuration** tab and set at least
   `edgar_user_agent` (your name + an email) to get EDGAR ingestion
   working. Add `finnhub_api_key` and `anthropic_api_key` when you have
   them -- see the main README for why that order.
4. Start the add-on. Watch the **Log** tab -- it logs which integrations
   are active/inactive at startup, and every relationship extracted,
   dossier update, and signal as it happens.
5. Open the add-on's **Ingress** tab (or `http://<host>:8100/` directly)
   for the dashboard.

See `ha-addons/smartboi/DOCS.md` (also shown in the add-on's Documentation
tab in HA) for the full list of configuration options.

## 3. (Optional) Turn on the price feed

Once you're ready to see actual hypothetical P&L rather than just
detected-and-logged signals: set `enable_ib_price_feed: true` and
`ib_host`/`ib_port` to the IB Gateway/TWS from step 1, then Restart (not
Rebuild -- this is a config-only change) the add-on.

This connection is **read-only**: it fetches historical bars for a last
price and reads account equity, nothing else. See the main repo's
`src/smartboi/prices.py` -- it contains no order-placement code, so there
is no way for this to submit a real order regardless of configuration.

## Where things are stored

Everything (logs, the relationship graph, dossiers, dedup index) is
written under this add-on's mapped `/config` share
(`smartboi_run/` and `smartboi_logs/`), visible via the Samba or File
Editor/Studio Code Server add-ons -- no `docker exec` needed to inspect
anything.
