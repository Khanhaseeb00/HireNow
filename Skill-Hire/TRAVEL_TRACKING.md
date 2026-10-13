# Hire Now travel location

Heading out now requires a fresh worker GPS fix (no older than 120 seconds; accuracy 1000 m or better). It records departure coordinates and enables a booking-scoped sharing session. Arrival continues to require accuracy 100 m or better, a fresh fix and distance within 300 m of the job pin. Heading out does not certify arrival.

The worker portal obtains positions while open, uploads about every 15 seconds, and shows sharing state. The hirer detail screen polls every 10 seconds, shows the worker marker and GPS accuracy circle, and identifies stale locations. Updates older than the last accepted reading are rejected. Only the assigned worker can upload and only the booking hirer can read; locations are not shared after arrival or when sharing is paused. Stop Sharing invalidates the session token so an old in-flight upload cannot resume sharing. Resume Sharing requires another fresh GPS fix. One worker can have one active journey at a time.

This is foreground web tracking. Browser suspension, a locked phone, denied permission or network loss may pause it. Native/background location has not been implemented or verified. No real user GPS update was simulated during automated tests.

## ETA

Without a routing provider, an ETA is shown only if GPS is fresh, accuracy is at most 100 m, measured speed is at least 1 m/s, and the job pin is within 150 km. It is labelled **rough travel time**, using straight-line distance divided by current measured speed. It excludes roads and traffic and is not a promised arrival time. Unknown speed, stale/coarse readings and a distant/cross-country demo pin produce **ETA unavailable**.

Google Routes traffic-aware driving ETA is implemented but disabled by default. Enable only after the operator approves sending worker and job coordinates to Google and provisions the API and billing:

- `TRAVEL_ROUTING_PROVIDER=google`
- `TRAVEL_ROUTING_COORDINATES_ALLOWED=true`
- `GOOGLE_ROUTES_API_KEY` set securely on the service

A key alone does not enable external coordinate transmission. No Google routing credentials or Render environment changes were made for this implementation. Route queries use a 60-second cache and a 5-second timeout. Failure falls back to the labelled speed estimate where valid, otherwise ETA unavailable. The configured route is driving, not walking or transit. Existing OpenStreetMap map tiles remain the UI map source.

References: [GPS speed semantics](https://developer.mozilla.org/en-US/docs/Web/API/GeolocationCoordinates/speed), [Google Routes computeRoutes](https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes).
