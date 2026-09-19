from jugaad_data.nse import NSELive
import pandas as pd

nse = NSELive()


def get_result_date(symbol: str) -> str:

    symbol = symbol.replace(".NS", "").upper()

    try:

        events = nse.company_event(symbol)

        if not isinstance(events, list):
            return "—"

        for item in events:

            purpose = str(
                item.get("purpose", "")
            ).lower()

            if (
                "result" in purpose
                or "financial" in purpose
                or "quarterly" in purpose
            ):

                dt = (
                    item.get("bm_date")
                    or item.get("date")
                )

                if dt:
                    return (
                        pd.to_datetime(dt)
                        .strftime("%d-%b-%Y")
                    )

    except Exception:
        pass

    return "—"
