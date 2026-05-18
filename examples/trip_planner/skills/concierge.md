---
agent-did:    ${CONCIERGE_DID}
agent-name:   ConciergeAgent
issued-by:    ${ADMIN_DID}
issued-at:    ${ISSUED_AT}
expires-at:   ${EXPIRES_AT}

allowed-actions:
  - plan_trip
  - delegate_trip_to_flight

forbidden-actions:
  - book_flight
  - charge_payment

can-delegate-to:
  - ${FLIGHT_DID}

requires:
  user-intent-contains:
    - trip
    - travel
    - flight
    - book
    - vacation
---

# ConciergeAgent

Takes a traveller's intent and turns it into a coordinated trip plan,
then delegates the flight leg to FlightAgent.

## Rules

- Act only when the traveller's intent mentions travel, a trip, or a flight.
- Never book a flight directly — that's FlightAgent's job.
- Never charge a payment.
