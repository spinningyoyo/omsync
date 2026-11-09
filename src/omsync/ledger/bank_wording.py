"""Bank wording that says how money moved, one pack per region.

Ported from Sure (we-promise/sure), AGPL-3.0, commit 0cb3284:
app/models/transaction/label_normalizer/dictionary.rb. The packs and their comments are Sure's;
only the syntax changed. Keep them in step with Sure rather than editing them here: wording omsync
adds lives in payment_methods.OMSYNC_WORDING.

A token is a literal phrase, matched case-insensitively at the start of a label, whole words
only, or an Rx where a literal cannot express the constraint that keeps it safe. Every pack is
always active, because labels come from the bank, not from the user's settings. A token earns its
place only if it cannot plausibly begin a real merchant name.

Deliberately absent, because a word boundary alone does not save them (from Sure): bare KAUF
(KAUFLAND), GA (GALERIA), BAR (BARILLA), DEB (DEBENHAMS), VIS (VISTAPRINT), BP (British
Petroleum), SO, DR, CR, CASH (CASH CONVERTERS), CREDIT (CREDIT UNION), bare PAYMENT and DEBIT,
GEA, BEA, IDEAL (Ideal Standard), bare MAESTRO, GIRO, bare ATM (ATM SOLUTIONS), CASHPOINT,
CHAPS, TED and DOC (TED Baker), and the bare two- and three-letter UK transaction-type codes.
"""

from typing import NamedTuple, TypedDict


class Rx(NamedTuple):
    """A token given as a regular expression rather than a literal phrase."""

    source: str


Token = str | Rx


class Pack(TypedDict):
    region: str
    rails: dict[str, list[Token]]


# Rails are tried in this order, whatever order a pack lists them in, and the order encodes real
# ambiguities: fee before withdrawal, or "ATM FEE" reads as cash taken out; loan_payment before
# refund, or "LOAN REPAYMENT" reads as money coming back; refund before card, direct_debit, and
# transfer, or "Refund Card Payment to ASOS" keeps the rail it reverses.
RAIL_ORDER = (
    "fee",
    "loan_payment",
    "refund",
    "withdrawal",
    "cheque",
    "card",
    "direct_debit",
    "transfer",
)

# What may sit between a consumed marker and the merchant. A connector must be followed by
# whitespace: a bare AT would turn "Card Payment AT&T" into a merchant called "T".
CONNECTORS = (
    r"EN\s+FAVEUR\s+DE",
    r"A\s+FAVORE\s+DI",
    r"A\s+FAVOR\s+DE",
    "FROM",
    "VOOR",
    "NAAR",
    "PARA",
    "VIA",
    "VON",
    "TO",
    "AT",
    "FOR",
    "DE",
)

PACKS: list[Pack] = [
    {
        "region": "fr",
        "rails": {
            "card": [
                Rx(
                    r"(?:ACHAT\s+|PAIEMENT\s+)?(?:FACT(?:URE)?\s+CARTE|CARTE\s+BANCAIRE|CARTE|CB)\b"
                ),
                Rx(r"(?:PAIEMENT\s+)?PSC\b"),
            ],
            "direct_debit": [Rx(r"(?:PRLV|PRELEVEMENT|PRELEVMNT)(?:\s+SEPA)?(?:\s+DE)?\b")],
            "transfer": [
                Rx(
                    r"(?:VIR(?:EMENT)?)(?:\s+(?:SEPA|INST(?:ANTANE)?|RECU|EMIS|PERMANENT))*(?:\s+(?:DE|POUR|EN\s+FAVEUR\s+DE))?\b"
                )
            ],  # noqa: E501
            # GAB is the Quebec spelling of DAB and reaches French-language
            # Canadian labels that no English pack covers.
            "withdrawal": [Rx(r"(?:RETRAIT)(?:\s+(?:DAB|GAB|CARTE|ESPECES))?\b")],
            "cheque": [Rx(r"(?:CHEQUE|CHQ)(?:\s+N[O°]?)?\b")],
            "fee": [
                Rx(r"(?:COMMISSION\s+D?\s*INTERVENTION|COMMISSION|FRAIS|AGIOS|COTIS(?:ATION)?)\b")
            ],
            # Lookahead: only the ECHEANCE/REMB marker is consumed. Unlike CB or
            # PRLV SEPA, the word PRET names what is being paid, so dropping it
            # would turn "ECHEANCE PRET ETUDIANT" into the bare "ETUDIANT".
            "loan_payment": [Rx(r"(?:ECH(?:EANCE)?|REMB(?:OURSEMENT)?)\b[\s:.\-]*(?=PRET\b)")],
            "refund": [Rx(r"(?:AVOIR|ANNULATION|REMBOURSEMENT|REMBT|REMB)\b")],
        },
    },
    {
        "region": "en_us",
        "rails": {
            "card": [
                "RECURRING PAYMENT AUTHORIZED ON",
                "PURCHASE AUTHORIZED ON",
                "RECURRING CARD PURCHASE",
                "POINT OF SALE PURCHASE",
                "CARD PURCHASE WITH PIN",
                "DEBIT CARD PURCHASE",
                "RETAIL PURCHASE",
                "CHECKCARD",
                "CHKCARD",
                "POS PURCHASE",
                "POS DEBIT",
                "PIN PURCHASE",
            ],
            "direct_debit": [
                "PREAUTHORIZED PAYMENT",
                "PREAUTHORIZED DEBIT",
                "AUTOMATIC PAYMENT",
                "ACH WITHDRAWAL",
                "ACH DEBIT",
            ],
            "transfer": [
                "ONLINE BANKING TRANSFER",
                "EXTERNAL TRANSFER",
                "INTERNET TRANSFER",
                "PAYROLL DEPOSIT",
                "ONLINE TRANSFER",
                "DIRECT DEPOSIT",
                "MOBILE DEPOSIT",
                "WIRE TRANSFER",
                "INCOMING WIRE",
                "OUTGOING WIRE",
                "ZELLE PAYMENT",
                "ZELLE",
                "TRANSFER",
            ],
            "withdrawal": [
                "ATM WITHDRAWAL AUTHORIZED ON",
                "ATM CASH WITHDRAWAL",
                "CASH WITHDRAWAL",
                "ATM WITHDRAWAL",
                "ATM WITHDRWL",
                "ATM DEBIT",
                "ATM CASH",
            ],
            "cheque": [
                "MOBILE CHECK DEPOSIT",
                "RETURNED CHECK",
                "CHECK DEPOSIT",
                "CHECK NUMBER",
                "CHECK PAID",
                "CHECK NO",
                # Bare CHECK only when a number follows or nothing does. Without the
                # lookahead it eats CHECK INTO CASH and CHECK 'N GO, which are payees.
                Rx(r"CHECK\b(?=[\s:.#-]*(?:\d|\Z))"),
            ],
            "fee": [
                "INTERNATIONAL TRANSACTION FEE",
                "NON-SUFFICIENT FUNDS FEE",
                "MONTHLY MAINTENANCE FEE",
                "FOREIGN TRANSACTION FEE",
                "INSUFFICIENT FUNDS FEE",
                "RETURNED PAYMENT FEE",
                "MONTHLY SERVICE FEE",
                "RETURNED ITEM FEE",
                "LATE PAYMENT FEE",
                "CASH ADVANCE FEE",
                "ATM OPERATOR FEE",
                "INTEREST CHARGED",
                "SERVICE CHARGE",
                "INTEREST CHARGE",
                "OVERDRAFT FEE",
                "ANNUAL FEE",
                "LATE FEE",
                "NSF FEE",
                "ATM FEE",
            ],
            "loan_payment": [
                "PERSONAL LOAN PAYMENT",
                "STUDENT LOAN PAYMENT",
                "INSTALLMENT PAYMENT",
                "AUTO LOAN PAYMENT",
                "MORTGAGE PAYMENT",
                "LOAN PAYMENT",
                "MORTGAGE PMT",
                "LOAN PMT",
            ],
            "refund": [
                "REFUND PURCHASE AUTHORIZED ON",
                "PURCHASE RETURN AUTHORIZED ON",
                "PURCHASE RETURN",
                "MERCHANT REFUND",
                "MERCHANT CREDIT",
                "CARD REFUND",
                "CHARGEBACK",
                "REVERSAL",
                "REFUND",
            ],
        },
    },
    {
        "region": "en_gb",
        "rails": {
            "card": [
                "VISA DEBIT PURCHASE",
                "CONTACTLESS PAYMENT",
                "VISA PURCHASE",
                "CARD PURCHASE",
                "CARD PAYMENT",
                # Safe bare because \b keeps it off POSTBANK, POSTE ITALIANE and
                # POSTFINANCE, and it is how NatWest and Lloyds label a card payment.
                "POS",
            ],
            "direct_debit": ["DIRECT DEBIT PAYMENT", "DIRECT DEBIT", "D/D"],
            "transfer": [
                "FASTER PAYMENTS RECEIPT",
                "BANK GIRO CREDIT",
                "FASTER PAYMENT",
                "STANDING ORDER",
                "BILL PAYMENT",
                "DIRECT CREDIT",
                "BACS",
                "S/O",
            ],
            "withdrawal": ["CASH WITHDRAWAL"],
            "cheque": ["CHEQUE DEPOSIT", "CHEQUE PAID", "CHEQUE NO"],
            "fee": [
                "NON-STERLING TRANSACTION FEE",
                "UNARRANGED OVERDRAFT FEE",
                "ARRANGED OVERDRAFT FEE",
                "PAPER STATEMENT FEE",
                "MONTHLY ACCOUNT FEE",
                "OVERDRAFT INTEREST",
                "BANK CHARGE",
                "ACCOUNT FEE",
            ],
            "loan_payment": ["MORTGAGE REPAYMENT", "LOAN INSTALMENT", "LOAN REPAYMENT"],
            "refund": ["CREDIT VOUCHER"],
        },
    },
    {
        "region": "en_ca",
        "rails": {
            "card": ["CONTACTLESS INTERAC PURCHASE", "INTERAC RETAIL PURCHASE", "INTERAC PURCHASE"],
            "direct_debit": ["PRE-AUTHORIZED DEBIT"],
            "transfer": [
                "INTERAC E-TRANSFER",
                "E-TRANSFER RECEIVED",
                "E-TRANSFER SENT",
                "E-TRANSFER",
            ],
            "withdrawal": ["ABM CASH WITHDRAWAL", "ABM WITHDRAWAL"],
        },
    },
    {
        "region": "de",
        "rails": {
            "card": [
                "KARTENZAHLUNG/-ABRECHNUNG",
                "KREDITKARTENABRECHNUNG",
                "MAESTRO KARTENZAHLUNG",
                "DEBITKARTENZAHLUNG",
                "KREDITKARTENUMSATZ",
                "KARTENZAHLUNG ONLINE",
                "SEPA-ELV-LASTSCHRIFT",
                "KAUF/DIENSTLEISTUNG",
                "KARTENABRECHNUNG",
                "KARTENVERFUEGUNG",
                "KARTENVERFÜGUNG",
                "GIROCARD-ZAHLUNG",
                "KARTENZAHLUNG",
                "TWINT ZAHLUNG",
                "POS-ZAHLUNG",
                "TWINT KAUF",
                "GIROCARD",
            ],
            "direct_debit": [
                "SEPA-FIRMENLASTSCHRIFT",
                "SEPA FIRMENLASTSCHRIFT",
                "SEPA-BASISLASTSCHRIFT",
                "SEPA BASISLASTSCHRIFT",
                "EINZUGSERMAECHTIGUNG",
                "EINZUGSERMÄCHTIGUNG",
                "LASTSCHRIFTEINZUG",
                "ABBUCHUNGSAUFTRAG",
                "FIRMENLASTSCHRIFT",
                "EINMALLASTSCHRIFT",
                "SEPA-LASTSCHRIFT",
                "SEPA LASTSCHRIFT",
                "FOLGELASTSCHRIFT",
                "BASISLASTSCHRIFT",
                "ERSTLASTSCHRIFT",
                "LASTSCHRIFT",
                "ABBUCHUNG",
                "LASTSCHR.",
                "LSV+",
            ],
            "transfer": [
                "UEBERWEISUNGSGUTSCHRIFT",
                "ÜBERWEISUNGSGUTSCHRIFT",
                "GUTSCHRIFT UEBERWEISUNG",
                "GUTSCHRIFT ÜBERWEISUNG",
                "ECHTZEIT-UEBERWEISUNG",
                "ECHTZEIT-ÜBERWEISUNG",
                "EINZELUEBERWEISUNG",
                "EINZELÜBERWEISUNG",
                "SAMMELUEBERWEISUNG",
                "SAMMELÜBERWEISUNG",
                "ONLINE-UEBERWEISUNG",
                "ONLINE-ÜBERWEISUNG",
                "SEPA-UEBERWEISUNG",
                "SEPA UEBERWEISUNG",
                "SEPA-ÜBERWEISUNG",
                "SEPA ÜBERWEISUNG",
                "LOHN/GEHALT/RENTE",
                "DAUERAUFTRAG",
                "UEBERWEISUNG",
                "ÜBERWEISUNG",
                "GEHALT/RENTE",
                "UMBUCHUNG",
                "UEBERTRAG",
                "ÜBERTRAG",
            ],
            "withdrawal": [
                "BARGELDBEZUG AM BANCOMAT",
                "AUSZAHLUNG GELDAUTOMAT",
                "SB-BARGELDBEHEBUNG",
                "BARGELDAUSZAHLUNG",
                "BANKOMATBEHEBUNG",
                "BARGELDBEHEBUNG",
                "BARAUSZAHLUNG",
                "BARGELDBEZUG",
                "GELDAUTOMAT",
                "GELDBEZUG",
                "ABHEBUNG",
            ],
            "cheque": [
                "VERRECHNUNGSSCHECK",
                "SCHECKEINREICHUNG",
                "SCHECKEINLOESUNG",
                "SCHECKEINLÖSUNG",
                "SCHECK",
            ],
            "fee": [
                "KONTOFUEHRUNGSGEBUEHR",
                "KONTOFÜHRUNGSGEBÜHR",
                "KONTOFUEHRUNGSENTGELT",
                "KONTOFÜHRUNGSENTGELT",
                "RECHNUNGSABSCHLUSS",
                "ENTGELTABRECHNUNG",
                "ENTGELTABSCHLUSS",
                "SOLLZINSEN",
                "GEBUEHREN",
                "GEBÜHREN",
                "ENTGELTE",
                "GEBUEHR",
                "GEBÜHR",
                "ENTGELT",
                "SPESEN",
            ],
            # DARLEHEN and HYPOTHEK are absent on purpose: Dutch and German lenders
            # trade under those words (De Hypotheker, Hypotheek Visie), so a bare
            # token would strip the payee rather than the marker.
            "loan_payment": [
                "DARLEHENSTILGUNG",
                "HYPOTHEKARZINSEN",
                "DARLEHENSZINSEN",
                "RATENZAHLUNG",
                "DARLEHENSRATE",
                "KREDITRATE",
                "ANNUITAET",
                "ANNUITÄT",
                "TILGUNG",
            ],
            "refund": [
                "GUTSCHRIFT KARTENZAHLUNG",
                "WIEDERGUTSCHRIFT",
                "RUECKUEBERWEISUNG",
                "RÜCKÜBERWEISUNG",
                "RUECKERSTATTUNG",
                "RÜCKERSTATTUNG",
                "RUECKLASTSCHRIFT",
                "RÜCKLASTSCHRIFT",
                "STORNIERUNG",
                "RUECKBUCHUNG",
                "RÜCKBUCHUNG",
                "ERSTATTUNG",
                "RETOURE",
                "STORNO",
            ],
        },
    },
    {
        "region": "es",
        "rails": {
            "card": [
                "COMPRA CON TARJETA",
                "PAGO CON TARJETA",
                "COMPRA TARJETA",
                "PAGO TARJETA",
                "COMPRA",
            ],
            "direct_debit": [
                "ADEUDO POR DOMICILIACION",
                "ADEUDO DOMICILIADO",
                "DOMICILIACION",
                "DOMICILIACIÓN",
                "ADEUDO",
                "RECIBO",
            ],
            "transfer": [
                "TRANSFERENCIA RECIBIDA",
                "TRANSFERENCIA EMITIDA",
                "TRANSFERENCIA",
                "TRASPASO",
                "BIZUM",
                "NOMINA",
                "NÓMINA",
            ],
            "withdrawal": [
                "DISPOSICION CAJERO",
                "RETIRADA EFECTIVO",
                "REINTEGRO CAJERO",
                "REINTEGRO",
            ],
            "fee": ["COMISION MANTENIMIENTO", "COMISION", "COMISIÓN"],
            "loan_payment": ["AMORTIZACION PRESTAMO", "CUOTA HIPOTECA", "CUOTA PRESTAMO"],
            "refund": [
                "ABONO POR DEVOLUCION",
                "DEVOLUCION",
                "DEVOLUCIÓN",
                "ANULACION",
                "ANULACIÓN",
            ],
        },
    },
    {
        "region": "it",
        "rails": {
            "card": [
                "PAGAMENTO TRAMITE POS",
                "PAGAMENTO CARTA",
                "PAGAMENTO POS",
                "ACQUISTO POS",
                "PAGOBANCOMAT",
            ],
            "direct_debit": [
                "ADDEBITO PREAUTORIZZATO",
                "ADDEBITO DIRETTO",
                "ADDEBITO SDD",
                "ADDEBITO RID",
                "ADDEBITO",
            ],
            "transfer": ["BONIFICO ISTANTANEO", "BONIFICO SEPA", "GIROCONTO", "BONIFICO"],
            "withdrawal": ["PRELIEVO BANCOMAT", "PRELIEVO ATM", "PRELIEVO"],
            "fee": [
                "SPESE TENUTA CONTO",
                "IMPOSTA DI BOLLO",
                "CANONE MENSILE",
                "COMMISSIONI",
                "COMMISSIONE",
            ],
            "loan_payment": ["RATA FINANZIAMENTO", "RATA PRESTITO", "RATA MUTUO"],
            "refund": ["ACCREDITO STORNO", "RIMBORSO", "STORNO"],
        },
    },
    {
        "region": "nl",
        "rails": {
            "card": [
                "BETALING MET KREDIETKAART",
                "BETAALAUTOMAATTRANSACTIE",
                "BETALING MET DEBETKAART",
                "BETAALPASTRANSACTIE",
                "AANKOOP BANCONTACT",
                "BETALING BANCONTACT",
                "BETAALAUTOMAAT",
                "BEA, BETAALPAS",
                "BEA, APPLE PAY",
                "BEA, GOOGLE PAY",
            ],
            "direct_debit": [
                "SEPA INCASSO ALGEMEEN DOORLOPEND",
                "SEPA INCASSO ALGEMEEN EENMALIG",
                "EUROPESE DOMICILIERING",
                "EUROPESE DOMICILIËRING",
                "SEPA INCASSO DOORLOPEND",
                "SEPA INCASSO EENMALIG",
                "DOORLOPENDE MACHTIGING",
                "SEPA DOMICILIERING",
                "SEPA DOMICILIËRING",
                "EUROPESE INCASSO",
                "SEPA INCASSO",
            ],
            "transfer": [
                "SEPA PERIODIEKE OVERBOEKING",
                "PERIODIEKE OVERSCHRIJVING",
                "EUROPESE OVERSCHRIJVING",
                "INSTANTOVERSCHRIJVING",
                "SEPA SPOEDOVERBOEKING",
                "PERIODIEKE OVERBOEKING",
                "SEPA OVERBOEKING",
                "OVERSCHRIJVING",
                "OVERBOEKING",
                "SEPA IDEAL",
            ],
            "withdrawal": [
                "GELDOPNEMING VIA AUTOMAAT",
                "GELDAFHALING BANCONTACT",
                "CONTANTE OPNAME",
                "GEA, BETAALPAS",
                "GELDAUTOMAAT",
                "GELDOPNEMING",
                "GELDAFHALING",
                "GELDOPNAME",
                "KASOPNAME",
            ],
            "cheque": ["CIRCULAIRE CHEQUE", "BANKCHEQUE"],
            "fee": [
                "BETAALPAKKET KOSTEN",
                "KOSTEN BETAALPAKKET",
                "TRANSACTIEKOSTEN",
                "BEHEERKOSTEN",
                "MAANDKOSTEN",
                "BANKKOSTEN",
                "DEBETRENTE",
            ],
            "loan_payment": [
                "AFLOSSING HYPOTHEEK",
                "HYPOTHEEKTERMIJN",
                "AFLOSSING KREDIET",
                "AFLOSSING LENING",
                "AFLOSSING",
            ],
            "refund": [
                "SEPA INCASSO TERUGBOEKING",
                "SEPA TERUGBOEKING",
                "TERUGSTORTING",
                "TERUGBOEKING",
                "STORNERING",
            ],
        },
    },
    {
        "region": "pt",
        "rails": {
            "card": [
                "PAGAMENTO COM CARTAO",
                "PAGAMENTO COM CARTÃO",
                "COMPRA COM CARTAO",
                "COMPRA COM CARTÃO",
                "COMPRA MULTIBANCO",
            ],
            "direct_debit": [
                "DEBITO AUTOMATICO",
                "DÉBITO AUTOMÁTICO",
                "DEBITO DIRETO",
                "DÉBITO DIRETO",
            ],
            "transfer": [
                "TRANSFERENCIA MB WAY",
                "TRANSFERÊNCIA",
                "PIX RECEBIDO",
                "PIX ENVIADO",
                "MB WAY",
                "PIX",
            ],
            "withdrawal": ["LEVANTAMENTO MB", "LEVANTAMENTO", "SAQUE"],
            "fee": ["IMPOSTO SELO", "COMISSAO", "COMISSÃO", "TARIFA"],
            "loan_payment": ["PRESTACAO EMPRESTIMO", "PRESTAÇÃO EMPRÉSTIMO", "PRESTACAO CREDITO"],
            "refund": ["ESTORNO", "DEVOLUCAO", "DEVOLUÇÃO"],
        },
    },
]
