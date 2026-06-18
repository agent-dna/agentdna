import logging
import random
from datetime import date, datetime, timedelta

from google.adk.agents import LlmAgent

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

def generate_karley_calendar() -> dict[str, list[str]]:
    """Generates a random calendar for Karley for the next 7 days."""
    logger.debug("generate_karley_calendar() START")

    calendar = {}
    today = date.today()
    possible_times = [f"{h:02}:00" for h in range(8, 21)]  
    logger.debug("Today=%s, possible_times=%r", today, possible_times)

    for i in range(7):
        current_date = today + timedelta(days=i)
        date_str = current_date.strftime("%Y-%m-%d")
        logger.debug("Generating availability for day offset=%d (%s)", i, date_str)

       
        available_slots = sorted(random.sample(possible_times, 8))
        logger.debug("Selected slots for %s: %r", date_str, available_slots)
        calendar[date_str] = available_slots

    logger.debug("Final generated Karley calendar=%r", calendar)
    print("Karley's calendar:", calendar)  

    logger.debug("generate_karley_calendar() END")
    return calendar


KARLEY_CALENDAR = generate_karley_calendar()
logger.debug("KARLEY_CALENDAR initialized: %r", KARLEY_CALENDAR)


def get_availability(start_date: str, end_date: str) -> str:
    """
    Checks Karley's availability for a given date range.

    Args:
        start_date: The start of the date range to check, in YYYY-MM-DD format.
        end_date: The end of the date range to check, in YYYY-MM-DD format.

    Returns:
        A string listing Karley's available times for that date range.
    """
    logger.debug(
        "get_availability() CALLED with start_date=%r, end_date=%r",
        start_date,
        end_date,
    )

    try:
        logger.debug("Parsing start_date and end_date")
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        logger.debug("Parsed start=%s, end=%s", start, end)

        if start > end:
            logger.warning(
                "Invalid date range: start (%s) is after end (%s)", start, end
            )
            return "Invalid date range. The start date cannot be after the end date."

        results = []
        delta = end - start
        logger.debug("Date range delta.days=%d", delta.days)

        for i in range(delta.days + 1):
            day = start + timedelta(days=i)
            date_str = day.strftime("%Y-%m-%d")
            available_slots = KARLEY_CALENDAR.get(date_str, [])
            logger.debug(
                "Checking availability for %s -> available_slots=%r",
                date_str,
                available_slots,
            )

            if available_slots:
                availability = (
                    f"On {date_str}, Karley is available at: "
                    f"{', '.join(available_slots)}."
                )
                results.append(availability)
            else:
                results.append(f"Karley is not available on {date_str}.")

        result_str = "\n".join(results)
        logger.debug("get_availability() RESULT:\n%s", result_str)
        return result_str

    except ValueError as e:
        logger.exception("ValueError in get_availability: %s", e)
        return (
            "Invalid date format. Please use YYYY-MM-DD for both start and end dates."
        )


def create_agent() -> LlmAgent:
    """Constructs the ADK agent for Karley."""
    logger.debug("create_agent() START")
    agent = LlmAgent(
        model="gemini-2.5-flash",
        name="Karley_Agent",
        instruction="""
            **Role:** You are Karley's personal scheduling assistant. 
            Your sole responsibility is to manage her calendar and respond to inquiries 
            about her availability for pickleball.

            **Core Directives:**

            *   **Check Availability:** Use the `get_karley_availability` tool to determine 
                    if Karley is free on a requested date or over a range of dates. 
                    The tool requires a `start_date` and `end_date`. If the user only provides 
                    a single date, use that date for both the start and end.
            *   **Polite and Concise:** Always be polite and to the point in your responses.
            *   **Stick to Your Role:** Do not engage in any conversation outside of scheduling. 
                    If asked other questions, politely state that you can only help with scheduling.
        """,
        tools=[get_availability],
    )
    logger.debug("LlmAgent created: %r", agent)
    logger.debug("create_agent() END")
    return agent