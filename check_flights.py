"""Cerca il volo A/R più economico Milano -> Tokyo su tutte le combinazioni di date
e salva il risultato in history.json. Usato dal workflow GitHub Actions.

Nota: Google Flights, in una ricerca A/R, mostra il prezzo totale ma i dettagli
della sola andata. I dettagli del ritorno arrivano da una seconda ricerca solo
ritorno, filtrata sulla stessa compagnia: sono indicativi, non garantiti."""

import itertools
import json
import os
import random
import time
from datetime import date, datetime
from pathlib import Path

from fast_flights import FlightQuery, Passengers, create_query, get_flights

ORIGINS = ["MXP"]
DESTINATIONS = ["NRT", "HND"]
DEPART = [date(2026, 12, d) for d in range(20, 25)]   # 20-24 dicembre
RETURN = [date(2027, 1, d) for d in range(8, 12)]     # 8-11 gennaio
CURRENCY = "EUR"
THRESHOLD = int(os.getenv("PRICE_THRESHOLD") or "0")  # 0 = nessuna soglia

HISTORY = Path(__file__).parent / "history.json"


def _dt(sd):
    return datetime(*sd.date, *sd.time)


def leg_info(f):
    """Orari, durata totale (voli + scali) e compagnie di una tratta."""
    segs = f.flights
    flown = sum(s.duration or 0 for s in segs)
    # Gli scali avvengono nello stesso aeroporto: arrivo e ripartenza sono nello
    # stesso fuso orario, quindi la differenza tra orari locali è corretta.
    layover = sum(max((_dt(b.departure) - _dt(a.arrival)).total_seconds() // 60, 0)
                  for a, b in zip(segs, segs[1:]))
    total = int(flown + layover)
    return {
        "airlines": f.airlines,
        "dep": _dt(segs[0].departure).strftime("%d/%m %H:%M"),
        "arr": _dt(segs[-1].arrival).strftime("%d/%m %H:%M"),
        "duration": f"{total // 60}h{total % 60:02d}",
        "minutes": total,
        "stops": len(segs) - 1,
        "via": [s.to_airport.code for s in segs[:-1]],
    }


def query(legs, trip):
    return create_query(flights=legs, trip=trip, seat="economy",
                        passengers=Passengers(adults=1), currency=CURRENCY, language="it")


def pause():
    time.sleep(random.uniform(2, 5))


def search():
    results, errors = [], []
    for o, d, dep, ret in itertools.product(ORIGINS, DESTINATIONS, DEPART, RETURN):
        q = query([FlightQuery(date=dep.isoformat(), from_airport=o, to_airport=d),
                   FlightQuery(date=ret.isoformat(), from_airport=d, to_airport=o)], "round-trip")
        try:
            flights = [f for f in get_flights(q) if f.price and f.price > 0 and f.flights]
            if flights:
                best = min(flights, key=lambda f: f.price)
                results.append({"origin": o, "dest": d, "depart": dep.isoformat(),
                                "return": ret.isoformat(), "price": best.price,
                                "out": leg_info(best), "url": q.url()})
        except Exception as e:  # noqa: BLE001
            import traceback
            fr = traceback.extract_tb(e.__traceback__)[-1]
            errors.append(f"{o}-{d} {dep}/{ret}: {type(e).__name__}: {e} @ {Path(fr.filename).name}:{fr.lineno} {fr.line}"[:300])
        pause()
    return results, errors


def return_leg(r):
    """Dettagli indicativi del ritorno: volo più breve della stessa compagnia."""
    q = query([FlightQuery(date=r["return"], from_airport=r["dest"], to_airport=r["origin"])], "one-way")
    try:
        opts = [f for f in get_flights(q) if f.flights]
    except Exception:  # noqa: BLE001
        return None
    same = [f for f in opts if set(f.airlines) & set(r["out"]["airlines"])]
    return leg_info(min(same, key=lambda f: leg_info(f)["minutes"])) if same else None


def fmt_leg(label, leg):
    if not leg:
        return f"**{label}:** dettagli non disponibili (verifica sul link)"
    via = f" via {', '.join(leg['via'])}" if leg["via"] else " diretto"
    return (f"**{label}:** {leg['dep']} → {leg['arr']} · {leg['duration']} totali · "
            f"{leg['stops']} scali{via} · {', '.join(leg['airlines'])}")


def main():
    results, errors = search()
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {"runs": []}
    prev_min = min((r["best"]["price"] for r in history["runs"] if r.get("best")), default=None)

    best = min(results, key=lambda r: r["price"]) if results else None
    if best:
        pause()
        best["ret"] = return_leg(best)
    history["runs"].append({"date": date.today().isoformat(), "best": best,
                            "found": len(results), "errors": len(errors)})
    HISTORY.write_text(json.dumps(history, indent=2, ensure_ascii=False))

    alert = bool(best) and (prev_min is None or best["price"] < prev_min
                            or (THRESHOLD and best["price"] <= THRESHOLD))

    lines = ["## Volo più economico Milano → Tokyo", ""]
    if best:
        lines += [f"**Prezzo A/R: {best['price']} {CURRENCY}** · {best['origin']}↔{best['dest']}",
                  f"Minimo precedente: {prev_min if prev_min is not None else 'n/d'} {CURRENCY}", "",
                  fmt_leg(f"Andata {best['depart']}", best["out"]), "",
                  fmt_leg(f"Ritorno {best['return']} (indicativo)", best["ret"]), "",
                  f"[Apri su Google Flights]({best['url']})", "",
                  "### Top 5 combinazioni",
                  "| Prezzo | Andata | Ritorno | Durata andata | Compagnia |",
                  "|---|---|---|---|---|"]
        for r in sorted(results, key=lambda r: r["price"])[:5]:
            lines.append(f"| {r['price']} | {r['depart']} {r['origin']}→{r['dest']} | {r['return']} | "
                         f"{r['out']['duration']} ({r['out']['stops']} scali) | {', '.join(r['out']['airlines'])} |")
    else:
        lines.append("Nessun risultato: probabile blocco o cambio di formato di Google Flights.")
    if errors:
        lines += ["", f"Errori: {len(errors)} su {len(ORIGINS)*len(DESTINATIONS)*len(DEPART)*len(RETURN)} ricerche",
                  *[f"- {e}" for e in errors[:5]]]
    report = "\n".join(lines)
    Path("report.md").write_text(report)
    print(report)

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"alert={'true' if alert else 'false'}\n")
            fh.write(f"failed={'true' if not results else 'false'}\n")
            fh.write(f"price={best['price'] if best else ''}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001  -> l'errore finisce nell'issue "Ricerca fallita"
        import traceback
        Path("report.md").write_text("Errore nello script:\n\n```\n" + traceback.format_exc() + "```")
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                fh.write("alert=false\nfailed=true\nprice=\n")
        print(traceback.format_exc())
