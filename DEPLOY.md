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

## 3. Picking up a new release

**Rebuild does not re-read `config.yaml`.** Home Assistant caches each
add-on's definition per repository, and Rebuild only rebuilds the Docker
image from that cached definition -- so a release that adds a new
configuration option will build fine and still show you the old
Configuration form, with the new option missing. This has caused confusion
more than once; it is a caching quirk, not a broken build.

To actually pick up a release:

1. `Settings -> Add-ons -> Add-on Store -> ⋮ -> Check for updates`. This
   re-reads `repository.yaml` and every add-on's `config.yaml`.
2. Open the SmartBoi add-on -- it should now offer **Update** to the new
   version. Use Update, not Rebuild.
3. Check the **Configuration** tab for any new options.

If step 1 doesn't surface the new version, remove and re-add the
repository URL under `⋮ -> Repositories` to force a full re-read.

**Options do not inherit repository defaults on an existing install.** A
value you set once is yours forever, even after the default in this repo
changes -- so after any release that recalibrates a default (`75/10`
universe bounds, `max_horizon_days: 21`), open the Configuration tab and
check it rather than assuming the new default applied.

A missing option is harmless in the meantime: the add-on only exports
environment variables for options actually present in its stored config,
so anything absent falls back to the default compiled into
`src/smartboi/config.py`. A new setting is therefore *active* from the
moment you update the image, whether or not the form shows it yet.

## 4. (Optional) Turn on the price feed

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
