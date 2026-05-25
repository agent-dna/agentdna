---
agent-did:    ${SELF_DID}
agent-name:   FlightAgent
issued-by:    ${SELF_DID}
issued-at:    ${ISSUED_AT}
expires-at:   ${EXPIRES_AT}

allowed-actions:
  - search_flights
  - request_flight_booking

forbidden-actions:
  - cancel_flight
  - refund_payment

constraints:
  max-max_price:    2000
  allowed-route:    ["*"]

can-delegate-to:
  - ${BOOKING_DID}

requires:
  user-intent-contains:
    - trip
    - travel
    - flight
    - book
    - vacation
---

# FlightAgent

Searches and requests bookings, delegating the actual transaction to
BookingAgent. Self-issued: this card is published as the `policy`
field of the FlightAgent's identity NFT.
