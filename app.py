import streamlit as st
import pandas as pd
from datetime import datetime
from rapidfuzz import fuzz
import re
from collections import Counter
import tempfile

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

# ==============================
# SAFE LOAD
# ==============================
@st.cache_data
def load_file(file):
    try:
        if file.name.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.strip()
        return df

    except Exception as e:
        st.error(f"❌ Error loading file: {e}")
        return pd.DataFrame()

# ==============================
# HELPERS
# ==============================
def clean_upc(series):
    try:
        return (
            series.astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"\D", "", regex=True)
        )
    except:
        return pd.Series([""] * len(series))

def clean_desc(series):
    return series.astype(str).str.lower().str.strip()

def normalize_upc_variants(upc):
    upc = re.sub(r"\D", "", str(upc))
    variants = set()

    if not upc:
        return variants

    variants.add(upc)

    if len(upc) == 12:
        variants.add(upc[:11])

    if len(upc) == 11:
        variants.add(upc)
        variants.add("0" + upc)

    if len(upc) == 10:
        v11 = "0" + upc
        variants.add(v11)
        variants.add(upc)

    if len(upc) < 12:
        variants.add(upc.zfill(12))

    return variants

# ==============================
# FAMILY INFERENCE (UNCHANGED)
# ==============================
def infer_family_smart(desc, product_df, product_desc_col, family_col):

    try:
        product_vocab = set(
            " ".join(product_df[product_desc_col].astype(str).str.lower()).split()
        )

        desc_clean = desc.lower()
        brand = desc_clean.split()[0] if desc_clean else ""

        filtered = product_df[
            product_df[product_desc_col].astype(str).str.lower().str.contains(brand, na=False)
        ]

        if filtered.empty:
            return "", ""

        scored = []
        for _, row in filtered.iterrows():
            score = fuzz.token_set_ratio(desc_clean, str(row[product_desc_col]).lower())
            if score >= 70:
                scored.append((score, row))

        if not scored:
            return "", ""

        best = max(scored, key=lambda x: x[0])[1]

        fam = best[family_col]

        best_type = ""
        if "Type" in product_df.columns:
            subset = product_df[product_df[family_col] == fam]
            if not subset.empty:
                best_type = subset["Type"].mode().iloc[0]

        return fam, best_type

    except:
        return "", ""

# ==============================
# UI
# ==============================
st.header("Upload Files")

adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store Assignment File", type=["xlsx", "csv"])

if adm_file and product_file and store_file:

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    if main_df.empty or product_df.empty or sf_df.empty:
        st.stop()

    st.success("Files loaded")

    product_upc1 = "ProductUPC"
    product_upc2 = "UnitUPC"
    product_desc = "Product Name"
    product_uid = "ProductId"
    product_family = "Family"

    st.header("Select ADM Columns")

    main_upc = st.selectbox("Main UPC", main_df.columns)
    main_desc = st.selectbox("Main Description", main_df.columns)
    main_store = st.selectbox("Main Store", main_df.columns)

    if st.button("🚀 Process Files"):

        try:
            progress = st.progress(0)
            status = st.empty()

            # CLEAN
            status.text("Cleaning...")
            main_df["desc_clean"] = clean_desc(main_df[main_desc])
            product_df["desc_clean"] = clean_desc(product_df[product_desc])

            product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
            product_df = product_df.explode("UPC_list")

            progress.progress(20)

            # ==============================
            # UPC MATCH
            # ==============================
            status.text("Matching UPCs...")

            product_lookup = {}

            for _, row in product_df.iterrows():
                upc = clean_upc(pd.Series([row["UPC_list"]])).iloc[0]
                variants = normalize_upc_variants(upc)

                for v in variants:
                    product_lookup.setdefault(v, []).append(row)

            def match_upc(row):
                try:
                    upc = clean_upc(pd.Series([row[main_upc]])).iloc[0]

                    if not upc:
                        return None, None

                    variants = normalize_upc_variants(upc)

                    matches = []
                    for v in variants:
                        if v in product_lookup:
                            matches.extend(product_lookup[v])

                    # 🔥 10-digit contains fallback
                    if not matches and len(upc) == 10:
                        contains = product_df[
                            product_df["UPC_list"].astype(str).str.contains(upc, na=False)
                        ]
                        if not contains.empty:
                            matches = contains.to_dict("records")

                    if not matches:
                        return None, None

                    uids = list(set([m[product_uid] for m in matches if product_uid in m]))
                    families = list(set([m[product_family] for m in matches if product_family in m]))

                    return uids, families

                except:
                    return None, None

            results = main_df.apply(match_upc, axis=1)

            merged = main_df.copy()
            merged["All Retail UIDs"] = results.apply(lambda x: x[0])
            merged["All Families"] = results.apply(lambda x: x[1])

            progress.progress(50)

            # ==============================
            # DESCRIPTION MATCH
            # ==============================
            status.text("Matching descriptions...")

            def fuzzy_match(row):
                try:
                    if isinstance(row["All Retail UIDs"], list):
                        return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"

                    desc = row["desc_clean"]

                    exact = product_df[product_df["desc_clean"] == desc]

                    if not exact.empty:
                        return (
                            list(set(exact[product_uid])),
                            list(set(exact[product_family])),
                            100,
                            "Exact Description Match"
                        )

                    return None, None, 0, "No Match"

                except:
                    return None, None, 0, "Error"

            results = merged.apply(fuzzy_match, axis=1)

            merged["All Retail UIDs"] = results.apply(lambda x: x[0])
            merged["All Families"] = results.apply(lambda x: x[1])
            merged["Match Score"] = results.apply(lambda x: x[2])
            merged["Match Type"] = results.apply(lambda x: x[3])

            progress.progress(75)

            # ==============================
            # STORE FAMILY VALIDATION
            # ==============================
            status.text("Validating store-family...")

            merged["Retail UID"] = merged["All Retail UIDs"].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["Family"] = merged["All Families"].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["store_family_key"] = (
                merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
            )

            sf_df["store_family_key"] = (
                sf_df["Store"].astype(str) + "|" + sf_df["Family"].astype(str)
            )

            valid_keys = set(sf_df["store_family_key"])
            merged["Valid Store-Family"] = merged["store_family_key"].isin(valid_keys)

            progress.progress(90)

            # ==============================
            # OUTPUT
            # ==============================
            status.text("Building output...")

            unmatched_df = merged[merged["Match Type"] == "No Match"][
                [main_upc, main_desc]
            ].drop_duplicates()

            unmatched_df["Family"], unmatched_df["Type"] = zip(*[
                infer_family_smart(x, product_df, product_desc, product_family)
                for x in unmatched_df[main_desc]
            ])

            with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
                temp_path = tmp.name

            with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
                merged.to_excel(writer, sheet_name="Full Output", index=False)
                unmatched_df.to_excel(writer, sheet_name="Unmatched", index=False)

            with open(temp_path, "rb") as f:
                file_bytes = f.read()

            progress.progress(100)
            status.text("Done")

            st.download_button(
                "Download",
                data=file_bytes,
                file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
            )

        except Exception as e:
            st.error(f"❌ Critical error: {e}")
