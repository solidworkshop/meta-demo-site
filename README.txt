E-commerce Simulator (REAL CAPI) — Quick Start
================================================

This build **posts to Meta Conversions API** when PIXEL_ID and ACCESS_TOKEN are set
(and DRY_RUN is not 1). If TEST_EVENT_CODE is set, activity will appear under
**Events Manager → Test Events**.

Run locally
-----------
1) Python 3.9+
2) `pip install flask requests`
3) `export PIXEL_ID=your_pixel_id`
4) `export ACCESS_TOKEN=your_system_user_token`
5) (optional) `export TEST_EVENT_CODE=XXXXXXX`  # copy from Events Manager
6) (optional) `export DRY_RUN=0`  # ensure real POSTs
7) `python app.py`
8) Open http://127.0.0.1:5000/?lite=1

Notes
-----
- Endpoint: https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events
- Params: access_token (+ test_event_code if provided)
- JSON: {"data":[<event>], "partner_agent":"ecomm-sim/1.0"}
- If you still see "No recent activity":
  * Make sure you're looking under **Test Events** if you set TEST_EVENT_CODE.
  * Confirm your token has `ads_management` + pixel permissions.
  * Ensure your machine has internet access and port 443 outbound.
  * Try a direct cURL:

curl -X POST "https://graph.facebook.com/v20.0/YOUR_PIXEL_ID/events?access_token=YOUR_TOKEN&test_event_code=YOUR_TEST_CODE" \
  -H "Content-Type: application/json" \
  -d '{"data":[{"event_name":"Purchase","event_time":'$(date +%s)',"action_source":"website","event_source_url":"http://127.0.0.1:5000","custom_data":{"currency":"USD","value":12.34}}]}'

- If cURL shows success but Events Manager doesn't, wait a minute and refresh Test Events.
- Set `DRY_RUN=1` to suppress outbound posts while testing UI.
