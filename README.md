# E-commerce Simulator v2.1.5

Adds **CAPI user_data toggles** for:
- Hashed Email (`em`)
- Client IP (`client_ip_address`)
- User Agent (`client_user_agent`)
- Browser ID (`fbp`)
- Click ID (`fbc`)

## How to use
1. Set `.env` with your `PIXEL_ID` and `ACCESS_TOKEN` (and optionally GA4 keys).
2. `pip install -r requirements.txt`
3. `python app.py`
4. In the UI under **Advanced Controls → CAPI User Data (toggles)**, set:
   - Check the fields you want to include for CAPI.
   - Enter an email if you want to send `em` (hashed on the server).
   - Load the page with a `?fbclid=...` to create `_fbc`, or simply fire a PageView to create `_fbp`. The page posts values to the server automatically.
5. Send events with the buttons or start auto stream.

Nothing else was removed or changed outside of the new toggles and fbp/fbc capture.
