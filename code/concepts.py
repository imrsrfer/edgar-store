"""Concept definitions: the mapping from a financial concept to its XBRL tag chain.

XBRL tag usage varies by filer, so every concept carries an ordered fallback chain.
The first tag that yields a value for a given company-period wins, and the winning
tag is recorded in ``source_tag`` so a wrong pick is always visible downstream.

Nothing here ever substitutes zero for a missing tag. A concept with no matching
tag resolves to null, which is a materially different statement from zero.
"""

from __future__ import annotations

# Period type of a concept, which decides how a fact's dates are interpreted.
INSTANT = "instant"
DURATION = "duration"

# Units we keep. Everything else in the source data is discarded.
UNIT_USD = "USD"
UNIT_SHARES = "shares"

# Fiscal periods.
FY = "FY"
QUARTERS = ("Q1", "Q2", "Q3", "Q4")

# A duration fact counts as annual/quarterly when its length falls in these
# day ranges. Fiscal calendars drift (52/53-week years, Saturday-nearest rules),
# so these are deliberately loose rather than exact.
ANNUAL_DAYS = (340, 400)
QUARTER_DAYS = (80, 100)
# 🔴 CUMULATIVE YEAR-TO-DATE SPANS, retained rather than dropped (2026-08-27).
# Filers report cash flow ONLY as YTD, so before this existed the sole surviving
# quarterly cash-flow fact was Q1 -- 113,702 Q1 rows against 22,774 Q2 -- and
# build_ttm's four-row sum was four Q1s from four different YEARS. These spans
# are what make a real TTM constructible: FY(prior) - YTD(prior) + YTD(current).
YTD2_DAYS = (170, 200)  # six months from fiscal-year start
YTD3_DAYS = (260, 290)  # nine months from fiscal-year start


class Concept:
    """One financial concept and the ordered tag chain that can supply it."""

    def __init__(
        self,
        name,
        period_type,
        chain,
        unit=UNIT_USD,
        components=None,
        ifrs_chain=None,
        partial_ok=False,
        never_alone=None,
        never_sole=None,
    ):
        self.name = name
        self.period_type = period_type
        self.chain = tuple(chain)
        self.unit = unit
        # For composite concepts: tags that are summed together, tried before
        # the flat chain.
        self.components = tuple(components) if components else ()
        # 🔴 COMPONENTS THAT ARE COMPLEMENTS, NEVER SUBSTITUTES. (Added
        # 2026-09-09.) A tag listed here may be ADDED to a component sum, but
        # may never BE the sum on its own. If every component a filer reports
        # is in this set, the component sum is abandoned and the flat chain is
        # tried instead; if the chain yields nothing either, the concept
        # resolves to NULL rather than to the complement on its own -- see
        # _resolve_one in build_facts for the five rows that settled that.
        #
        # Why this had to exist before PaymentsToAcquireIntangibleAssets could
        # be added: _resolve_components returns as soon as ANY component
        # matches, and resolve() then returns WITHOUT trying the chain. So a
        # filer reporting an intangibles line plus a BROAD
        # PaymentsToAcquireProductiveAssets total -- and no itemised PP&E leg --
        # would have had its ENTIRE capex replaced by the intangibles line.
        # Measured on the 2026-08-25 archive: 85 filers, including VERIZON
        # (capex 16,658,000,000 -> 450,000,000), FIRSTENERGY (4,705,000,000 ->
        # 2,000,000) and DELTA (4,499,000,000 -> 66,500,000). Understated capex
        # OVERSTATES FCF, so that is the fail-open direction, and it would have
        # been a far bigger defect than the one being fixed.
        #
        # PaymentsToAcquireOtherPropertyPlantAndEquipment is in this set too.
        # The 2026-08-19 comment beside it already SAID it "must never be
        # reachable as a lone winner" -- but nothing enforced that, so the same
        # substitution was reachable there all along. That comment described an
        # intention the code did not implement; here it becomes real.
        self.never_alone = frozenset(never_alone or ())
        # 🔴 A STRICTER TIER. never_alone means "loses to the flat chain"; a tag
        # here additionally may not resolve the concept AT ALL on its own, even
        # when the chain yields nothing. The difference is whether the tag is a
        # plausible SOLE disclosure.
        #
        # PaymentsToAcquireOtherPropertyPlantAndEquipment is a PP&E line, so a
        # filer reporting only that is making a real, if unusual, capex
        # disclosure -- LLY discloses 7,841,000,000 that way, ALK 309,000,000,
        # ADP 196,600,000. Discarding those cost 51 rows a capex they genuinely
        # had, and moved 14 of them from pass to unevaluable.
        #
        # PaymentsToAcquireIntangibleAssets is NOT a PP&E line. When it is the
        # only leg captured for a company that obviously owns plant, that is
        # evidence of INCOMPLETE CAPTURE, not of a company without PP&E. Left
        # usable alone it filled five rows whose capex was correctly null --
        # VZ, STM, INCY, SUNB, APP, all gate0_status="unknown" because their
        # latest filing is a 10-Q/20-F/40-F -- with an annual capex invented
        # from a partial period: VZ 450,000,000 against a real ~17,000,000,000,
        # and STM's FCF 2,059,000,000 on a 93,000,000 capex.
        self.never_sole = frozenset(never_sole or ())
        # True when a subset of the components is the NORMAL case rather than
        # a defect. total_debt has two components and a filer missing one is
        # notable, so it is marked "(partial)". capex has nine disjoint legs
        # and almost every filer uses one or two, so marking those "(partial)"
        # would label the ordinary case as degraded and make the marker
        # useless. See _resolve_components in build_facts.
        self.partial_ok = bool(partial_ok)
        # ifrs-full fallback, tried only after the whole us-gaap chain (and
        # components) come up empty for a company-period. Foreign private
        # issuers filing 20-F/40-F often report in a non-USD currency, so
        # these tags accept any currency unit rather than USD only -- see
        # build_facts._matching. Never mixed into ``chain``: a coincidental
        # same-named tag in the wrong taxonomy must not silently match.
        self.ifrs_chain = tuple(ifrs_chain) if ifrs_chain else ()

    @property
    def is_instant(self):
        return self.period_type == INSTANT

    @property
    def all_tags(self):
        """Every tag this concept might read, for the parser's whitelist."""
        return tuple(self.components) + self.chain + self.ifrs_chain

    def __repr__(self):
        return f"Concept({self.name!r}, {self.period_type!r}, {len(self.chain)} tags)"


CONCEPTS = (
    Concept(
        "equity",
        INSTANT,
        [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "MembersEquity",
            "PartnersCapital",
            "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
            "CommonStockholdersEquity",
        ],
        # Parent-only first, matching the us-gaap chain's own preference
        # (StockholdersEquity, the parent figure, before the NCI-inclusive
        # fallback) -- tangible_book should be computed on a consistent
        # equity basis regardless of taxonomy.
        ifrs_chain=["EquityAttributableToOwnersOfParent", "Equity"],
    ),
    Concept("goodwill", INSTANT, ["Goodwill"], ifrs_chain=["Goodwill"]),
    Concept(
        "intangibles",
        INSTANT,
        [
            "IntangibleAssetsNetExcludingGoodwill",
            "FiniteLivedIntangibleAssetsNet",
        ],
        ifrs_chain=["IntangibleAssetsOtherThanGoodwill"],
    ),
    # Summed from the two components when available; the flat chain is the fallback.
    Concept(
        "total_debt",
        INSTANT,
        ["DebtLongtermAndShorttermCombinedAmount"],
        components=["LongTermDebtNoncurrent", "LongTermDebtCurrent"],
        # "Borrowings" (61% of a 145-company IFRS sample) is the single
        # combined tag; LongtermBorrowings (69%) is the fallback for filers
        # (e.g. Spotify) that split current/noncurrent instead -- there is no
        # ifrs equivalent of the components= summing mechanism above, so this
        # slightly understates total_debt for that group (current portion
        # excluded) rather than needing new sum-of-ifrs-tags plumbing for a
        # non-load-bearing concept.
        ifrs_chain=["Borrowings", "LongtermBorrowings"],
    ),
    Concept(
        "cash",
        INSTANT,
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ],
        ifrs_chain=["CashAndCashEquivalents"],
    ),
    Concept(
        "revenue",
        DURATION,
        [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            "RegulatedAndUnregulatedOperatingRevenue",
            "HealthCareOrganizationRevenue",
            "ContractsRevenue",
            "OilAndGasRevenue",
            "TotalRevenuesAndOtherIncome",
            "RevenuesNetOfInterestExpense",
            "InterestAndDividendIncomeOperating",
            "PremiumsEarnedNet",
            "RealEstateRevenueNet",
        ],
        ifrs_chain=["Revenue", "RevenueFromContractsWithCustomers"],
    ),
    Concept(
        "net_income",
        DURATION,
        [
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "IncomeLossFromContinuingOperations",
        ],
        ifrs_chain=["ProfitLoss", "ProfitLossAttributableToOwnersOfParent"],
    ),
    Concept(
        "operating_income",
        DURATION,
        ["OperatingIncomeLoss"],
        ifrs_chain=["ProfitLossFromOperatingActivities"],
    ),
    Concept(
        "ocf",
        DURATION,
        [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
        ifrs_chain=["CashFlowsFromUsedInOperatingActivities"],
    ),
    # 🔴 capex is a SUM, not a first-match chain (changed 2026-08-19).
    #
    # It was a flat chain, and the chain is what broke it. The tags below are
    # DISJOINT line items on the investing-activities statement -- a filer can
    # and does report several of them at once -- so "first tag that yields a
    # value wins" silently counted one leg and dropped the rest. Three real
    # failures, all found in the same 2026-08-19 audit of store rows that had
    # cleared every Gate 0 quality leg:
    #
    #   NOG  -- PaymentsToAcquireOtherPropertyPlantAndEquipment sat THIRD in the
    #           chain and matched at $0.76M, so the two oil-and-gas development
    #           tags at positions 8 and 9 were never read. An E&P's capex read
    #           as $0.8M against $2.48B of revenue; FCF/share and P/FCF (1.9x,
    #           a 53% FCF yield) were nonsense, and the fail_fcf test -- the
    #           load-bearing "does this company generate real cash" claim --
    #           could not fire.
    #   SKYW -- aircraft purchases tagged separately from the PP&E line; capex
    #           read $32M against $940M of OCF for an airline.
    #   LRN  -- capitalized curriculum/software tagged separately; capex read
    #           $0.59M against $2.52B of revenue.
    #
    # Understated capex overstates FCF, and FCF/share after SBC is the master
    # metric of the growth screen. A wrong number here propagates to fcf,
    # fcf_after_sbc, fcf_per_share, both FCF CAGR legs, p_fcf_after_sbc and
    # ev_fcf_after_sbc -- and it fails OPEN, flattering the company, which is
    # the direction a quality gate must never fail in.
    #
    # The two "ProductiveAssets" tags stay in the flat chain rather than the
    # component sum: they are BROAD TOTALS that already include PP&E for the
    # filers that use them, so summing them with the PP&E leg double-counts.
    # They are the fallback for a filer that reports no itemised leg at all.
    Concept(
        "capex",
        DURATION,
        [
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForProceedsFromProductiveAssets",
        ],
        components=[
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireOilAndGasProperty",
            "PaymentsToExploreAndDevelopOilAndGasProperties",
            "PaymentsToAcquireMachineryAndEquipment",
            "PaymentsForCapitalImprovements",
            "PaymentsToAcquireBuildings",
            "PaymentsToAcquireRealEstate",
            "PaymentsToDevelopRealEstateAssets",
            "PaymentsToDevelopSoftware",
            "PaymentsToAcquireSoftware",
            # The residual "other PP&E" line. It is a COMPLEMENT to the PP&E
            # tag above, never a substitute for it, which is exactly why it
            # must be summed and must never resolve capex on its own. That last
            # clause is now ENFORCED, via never_alone below, rather than merely
            # asserted here -- it was not, from 2026-08-19 until 2026-09-09.
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            # 🔴 CAPITALISED INTANGIBLES ARE CAPEX. (Added 2026-09-09.) A
            # disjoint investing line that CO-REPORTS with PP&E -- the same
            # property that forced the 2026-08-19 sum -- so a first-match chain
            # would have dropped whichever leg it did not pick. OMCL was found
            # by hand; the archive says it is not a handful of software
            # capitalisers but a systematic hole: 2,708 filers carry the tag,
            # and 2,032 of them ALSO report an itemised PP&E leg, i.e. their
            # capex is currently UNDERSTATED rather than missing. A further 127
            # report it with no PP&E leg at all, where capex is null today.
            # Median understatement is 5.8% of current capex, p75 67.5%,
            # p90 797% -- the tail is where the damage is, because understated
            # capex overstates FCF and FCF/share is the master metric.
            #
            # The two sibling tags checked at the same time --
            # PaymentsToAcquireFiniteLivedIntangibleAssets and
            # PaymentsToAcquireIntangibleAssetsExcludingGoodwill -- appear on
            # ZERO filers anywhere in the archive (substring match, any form,
            # any period, any unit). They are deliberately NOT listed: a tag
            # that never occurs is not harmless, it is a claim of coverage that
            # nothing tests, and the next reader cannot tell it from a live one.
            "PaymentsToAcquireIntangibleAssets",
        ],
        partial_ok=True,
        # See Concept.never_alone. Both of these are complements to the PP&E
        # leg; neither may resolve capex by itself while a broad total exists.
        never_alone=(
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "PaymentsToAcquireIntangibleAssets",
        ),
        # ...and intangibles alone are not a capex disclosure at all. See
        # Concept.never_sole for the five rows that drew this line.
        never_sole=("PaymentsToAcquireIntangibleAssets",),
        # Second tag verified against Copa Holdings (CIK 1345105, 20-F): an
        # airline reporting PP&E, intangibles and investment-property
        # purchases as one combined investing-activities line rather than
        # the narrower PP&E-only tag above. Confirmed present in 14% of a
        # 145-company IFRS sample -- a real, recurring variant, not a
        # one-off; tried second so the narrower tag still wins where it
        # exists.
        ifrs_chain=[
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
        ],
    ),
    Concept(
        "sbc",
        DURATION,
        [
            "ShareBasedCompensation",
            "AllocatedShareBasedCompensationExpense",
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost1",
        ],
        # The user's original candidate, ShareBasedPaymentsExpense, does not
        # exist in the archive (0/145 IFRS filers). These two do (86/145
        # combined) -- AdjustmentsForSharebasedPayments is the cash-flow-
        # statement non-cash add-back, the closest IFRS analogue of
        # ShareBasedCompensation.
        ifrs_chain=[
            "AdjustmentsForSharebasedPayments",
            "ExpenseFromSharebasedPaymentTransactionsWithEmployees",
        ],
    ),
    Concept(
        "acquisitions",
        DURATION,
        ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
        # The user's candidate was missing the ClassifiedAsInvestingActivities
        # suffix; without it the tag does not exist in the archive.
        ifrs_chain=[
            "CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities"
        ],
    ),
    Concept(
        "buybacks",
        DURATION,
        ["PaymentsForRepurchaseOfCommonStock"],
        # The user's candidate, PaymentsForRepurchaseOfEntitysOwnShares, does
        # not exist in the archive; PurchaseOfTreasuryShares does (27/145).
        ifrs_chain=["PurchaseOfTreasuryShares"],
    ),
    Concept(
        "dividends",
        DURATION,
        ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
        ifrs_chain=["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"],
    ),
    Concept(
        "dep_amort",
        DURATION,
        [
            "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
        ],
        # DepreciationExpense verified against TSMC, which reports
        # depreciation and amortisation as two separate tags rather than a
        # combined one; using D alone still understates D+A for that group,
        # but raises resolution from 61% to 74% of a 145-company IFRS sample
        # (this concept isn't load-bearing anywhere in Gate 0).
        ifrs_chain=[
            "DepreciationAmortisationExpense",
            "AdjustmentsForDepreciationAndAmortisationExpense",
            "DepreciationExpense",
        ],
    ),
    Concept(
        "tax_expense",
        DURATION,
        ["IncomeTaxExpenseBenefit"],
        ifrs_chain=["IncomeTaxExpenseContinuingOperations", "TaxExpenseIncome"],
    ),
    Concept(
        "pretax_income",
        DURATION,
        [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        ],
        ifrs_chain=["ProfitLossBeforeTax"],
    ),
    Concept(
        "shares_diluted",
        DURATION,
        ["WeightedAverageNumberOfDilutedSharesOutstanding"],
        unit=UNIT_SHARES,
        ifrs_chain=["DilutedAverageSharesOutstanding", "WeightedAverageShares"],
    ),
)

CONCEPTS_BY_NAME = {c.name: c for c in CONCEPTS}

# Flat whitelist of every us-gaap tag the parser needs to retain.
WANTED_TAGS = frozenset(tag for c in CONCEPTS for tag in c.all_tags)

# Tag -> the concepts that may source from it (a tag can serve more than one).
TAG_TO_CONCEPTS = {}
for _c in CONCEPTS:
    for _tag in _c.all_tags:
        TAG_TO_CONCEPTS.setdefault(_tag, []).append(_c.name)

# Concepts a Gate 0 verdict genuinely depends on. Anything missing from this set
# for a given company is reported in data_quality.csv rather than silently passed.
#
# goodwill/intangibles are here despite usually being a legitimate zero (most
# filers with no acquisition history never file the tag at all): tangible_book
# is a load-bearing test and strictly requires both to be present, so their
# absence is exactly the kind of silent-untestable case this list exists to
# surface. Analysis against the original (pre-widening, pre-liveness-filter)
# universe found them at 0% missing; that turned out to be a property of that
# narrower, more-established universe, not a fact about the concept.
REQUIRED_CONCEPTS = (
    "equity",
    "goodwill",
    "intangibles",
    "revenue",
    "net_income",
    "operating_income",
    "ocf",
    "capex",
    "sbc",
)


def accepted_unit(concept_name):
    """Unit a concept must be reported in, so a share count in USD is rejected."""
    concept = CONCEPTS_BY_NAME.get(concept_name)
    return concept.unit if concept else UNIT_USD
