import streamlit as st
import pandas as pd
from datetime import datetime
from rapidfuzz import fuzz
import re
import tempfile

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

# ==============================
# LOAD
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
        st.error(f"Error loading file: {e}")
        return pd.DataFrame()

# ==============================
# HELPERS
# ==============================
def clean_upc(series):
    return (
        series.astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\D", "", regex=True)
    )

def clean_desc(series):
    return series.astype(str).str.lower().str.strip()

def safe_first(x):
    if isinstance(x, list) and len(x) > 0:
        return x[0]
    return None

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

    main_upc = st.selectbox("UPC", main_df.columns)
    main_desc = st.selectbox("Description", main_df.columns)
    main_store = st.selectbox("Store", main_df.columns)

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        # ==============================
        # CLEAN
        # ==============================
        status.text("Cleaning...")

        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])

        main_df["UPC_clean"] = clean_upc(main_df[main_upc])

        progress.progress(10)

        # ==============================
        # BUILD PRODUCT TABLE
        # ==============================
        status.text("Preparing product lookup...")

        product_df_1 = product_df[[product_uid, product_family, product_desc, product_upc1]].copy()
        product_df_1["UPC_clean"] = clean_upc(product_df_1[product_upc1])
        product_df_1["UPC_SOURCE"] = "ProductUPC"

        product_df_2 = product_df[[product_uid, product_family, product_desc, product_upc2]].copy()
        product_df_2["UPC_clean"] = clean_upc(product_df_2[product_upc2])
        product_df_2["UPC_SOURCE"] = "UnitUPC"

        product_df_all = pd.concat([product_df_1, product_df_2], ignore_index=True)
        product_df_all = product_df_all.dropna(subset=["UPC_clean"])

        progress.progress(25)

        # ==============================
        # UPC MATCH
        # ==============================
        status.text("Matching UPCs...")

        main_df["_orig_index"] = main_df.index

        merged = main_df.merge(
            product_df_all,
            on="UPC_clean",
            how="left"
        )

        agg = merged.groupby("_orig_index").agg({
            product_uid: lambda x: list(pd.unique(x.dropna())),
            product_family: lambda x: list(pd.unique(x.dropna())),
            "UPC_SOURCE": lambda x: list(pd.unique(x.dropna()))
        })

        main_df["All Retail UIDs"] = main_df["_orig_index"].map(agg[product_uid])
        main_df["All Families"] = main_df["_orig_index"].map(agg[product_family])

        def format_source(x):
            if isinstance(x, list):
                return x[0] if len(x) == 1 else "Mixed"
            return "No Match"

        main_df["Match Source"] = main_df["_orig_index"].map(
            agg["UPC_SOURCE"].apply(format_source)
        )

        progress.progress(50)

        # ==============================
        # DESCRIPTION MATCH
        # ==============================
        status.text("Matching descriptions...")

        unmatched_mask = main_df["All Retail UIDs"].isna()

        product_desc_dedup = product_df.drop_duplicates(subset=["desc_clean"])

        product_desc_map = product_desc_dedup.set_index("desc_clean")[
            [product_uid, product_family]
        ].to_dict("index")

        def map_desc(x):
            return product_desc_map.get(x)

        desc_matches = main_df.loc[unmatched_mask, "desc_clean"].map(map_desc)

        main_df.loc[unmatched_mask, "All Retail UIDs"] = desc_matches.apply(
            lambda x: [x[product_uid]] if isinstance(x, dict) else None
        )

        main_df.loc[unmatched_mask, "All Families"] = desc_matches.apply(
            lambda x: [x[product_family]] if isinstance(x, dict) else None
        )

        main_df["Match Score"] = main_df["All Retail UIDs"].apply(
            lambda x: 100 if isinstance(x, list) else 0
        )

        main_df["Match Type"] = main_df["All Retail UIDs"].apply(
            lambda x: "UPC Match" if isinstance(x, list) else "No Match"
        )

        progress.progress(75)

        # ==============================
        # STORE FAMILY VALIDATION
        # ==============================
        status.text("Validating store-family...")

        main_df["Retail UID"] = main_df["All Retail UIDs"].apply(safe_first)
        main_df["Family"] = main_df["All Families"].apply(safe_first)

        main_df["store_family_key"] = (
            main_df[main_store].astype(str) + "|" + main_df["Family"].astype(str)
        )

        sf_df["store_family_key"] = (
            sf_df["Store"].astype(str) + "|" + sf_df["Family"].astype(str)
        )

        valid_keys = set(sf_df["store_family_key"])
        main_df["Valid Store-Family"] = main_df["store_family_key"].isin(valid_keys)

        progress.progress(90)

        # ==============================
        # OUTPUTS
        # ==============================
        status.text("Building output...")

        summary = main_df["Match Type"].value_counts().reset_index()
        summary.columns = ["Match Type", "Count"]

        good_df = main_df[
            (main_df["Retail UID"].notna()) &
            (main_df["Valid Store-Family"])
        ][[main_store, "Retail UID"]].drop_duplicates()
        good_df.columns = ["Store", "Retail UID"]

        invalid_df = main_df[
            (main_df["Retail UID"].isna()) |
            (~main_df["Valid Store-Family"])
        ][[main_store, main_upc, main_desc]]
        invalid_df.columns = ["Store", "UPC", "Description"]

        unmatched_df = main_df[main_df["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ].drop_duplicates()
        unmatched_df.columns = ["UPC", "Description"]

        invalid_sf_df = main_df[~main_df["Valid Store-Family"]][
            [main_store, "Family"]
        ].drop_duplicates()
        invalid_sf_df.columns = ["Store", "Family"]

        # TEMPLATE
        template_df = pd.DataFrame({
            "ProductId": unmatched_df["UPC"],
            "UnitId": "",
            "CaseId": "",
            "Product Name": unmatched_df["Description"],
            "Type": "",
            "Family": "",
            "Group": "",
            "ProductUPC": unmatched_df["UPC"],
            "UnitUPC": "",
            "CaseUPC": "",
            "Active": "true",
            "Products/Case": "",
            "Units/Product": "",
            "Unit Size": "",
            "Unit Measure": "",
            "ParentId": "",
            "Family Head": "false"
        })

        # MULTIPLE MATCHES
        multi_match_df = main_df[
            main_df["All Retail UIDs"].apply(lambda x: isinstance(x, list) and len(x) > 1)
        ][[main_upc, main_desc, "All Retail UIDs"]].copy()

        multi_match_df.columns = ["UPC", "Description", "Retail UIDs"]

        multi_match_df["Retail UIDs"] = multi_match_df["Retail UIDs"].apply(lambda x: sorted(set(x)))

        max_len = multi_match_df["Retail UIDs"].apply(len).max() if not multi_match_df.empty else 0

        for i in range(max_len):
            multi_match_df[f"Retail UID {i+1}"] = multi_match_df["Retail UIDs"].apply(
                lambda x: x[i] if len(x) > i else None
            )

        multi_match_df = multi_match_df.drop(columns=["Retail UIDs"])

        # EXPORT
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            temp_path = tmp.name

        with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
            main_df.to_excel(writer, sheet_name="Full Output", index=False)
            summary.to_excel(writer, sheet_name="Summary", index=False)
            good_df.to_excel(writer, sheet_name="Good To Go", index=False)
            invalid_df.to_excel(writer, sheet_name="Invalid For Portal", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched Products", index=False)
            multi_match_df.to_excel(writer, sheet_name="Multiple Matches", index=False)
            invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)
            template_df.to_excel(writer, sheet_name="Product Template", index=False)

        with open(temp_path, "rb") as f:
            file_bytes = f.read()

        progress.progress(100)
        status.text("Done")

        st.download_button(
            "Download",
            data=file_bytes,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
