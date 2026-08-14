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


class Concept:
    """One financial concept and the ordered tag chain that can supply it."""

    def __init__(self, name, period_type, chain, unit=UNIT_USD, components=None, ifrs_chain=None):
        self.name = name
        self.period_type = period_type
        self.chain = tuple(chain)
        self.unit = unit
        # For composite concepts (currently only total_debt): tags that are
        # summed together, tried before the flat chain.
        self.components = tuple(components) if components else ()
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
    Concept(
        "capex",
        DURATION,
        [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "PaymentsForCapitalImprovements",
            "PaymentsToAcquireMachineryAndEquipment",
            "PaymentsToAcquireRealEstate",
            "PaymentsToDevelopRealEstateAssets",
            "PaymentsToAcquireOilAndGasProperty",
            "PaymentsToExploreAndDevelopOilAndGasProperties",
            "PaymentsForProceedsFromProductiveAssets",
            "PaymentsToAcquireBuildings",
        ],
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
