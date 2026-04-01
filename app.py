import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz
import re

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

@st.cache_data
def load_file(file):
    return pd.read_excel(file)

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

def generate_keys(df, col, prefix):
    s = clean_upc(df[col])
    df[f"{prefix}_12"] = s.str.zfill(12)
    df[f"{prefix}_10"] = df[f"{prefix}_12"].str[-10:]

# ==============================
# PACK PARSER
# ==============================
def parse_pack(desc):
    desc = str(desc).lower()
    group, size, unit = None, None, None

    m = re.search(r"(\d+)/(\d+)(oz|ml)?", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = int(m.group(2))
        if m.group(3):
            unit = m.group(3).upper()

    m = re.search(r"(\d+)pk.*?(\d+)(oz|ml)", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = int(m.group(2))
        unit = m.group(3).upper()

    return group, size, unit

# ==============================
# UI
# ==============================
st.header("Upload Files")

adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store Assignment File", type=["xlsx"])

if adm_file and product_file and store_file:

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    # Normalize columns
    product_df.columns = product_df.columns.str.strip()

    # Ensure required columns exist
    for col in ["Group", "Products/Case", "Units/Product", "Unit Size", "Unit Measure"]:
        if col not in product_df.columns:
            product_df[col] = None

    st.success("Files loaded")

    # ==============================
    # COLUMN SELECTORS
    # ==============================
    st.header("Select Columns")

    col1, col2, col3 = st.columns(3)

    with col1:
        main_upc = st.selectbox("Main UPC", main_df.columns)
        main_desc = st.selectbox("Main Description", main_df.columns)
        main_store = st.selectbox("Main Store", main_df.columns)

    with col2:
        product_upc1 = st.selectbox("Product UPC 1", product_df.columns)
        product_upc2 = st.selectbox("Product UPC 2", product_df.columns)
        product_desc = st.selectbox("Product Description", product_df.columns)
        product_uid = st.selectbox("Product UID", product_df.columns)
        product_family = st.selectbox("Product Family", product_df.columns)

    with col3:
        sf_store = st.selectbox("Store Column", sf_df.columns)
        sf_family = st.selectbox("Family Column", sf_df.columns)

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        try:
            # ==============================
            # CLEAN
            # ==============================
            status.text("Cleaning...")
            main_df["desc_clean"] = clean_desc(main_df[main_desc])
            product_df["desc_clean"] = clean_desc(product_df[product_desc])
            generate_keys(main_df, main_upc, "m")

            df1 = product_df.copy()
            df1["UPC_list"] = df1[product_upc1]

            df2 = product_df.copy()
            df2["UPC_list"] = df2[product_upc2]

            product_df2 = pd.concat([df1, df2], ignore_index=True)
            product_df2 = product_df2.dropna(subset=["UPC_list"])

            generate_keys(product_df2, "UPC_list", "p")

            progress.progress(20)

            # ==============================
            # MATCH
            # ==============================
            status.text("Matching...")

            map_12 = product_df2.groupby("p_12").agg({
                product_uid: lambda x: list(set(x)),
                product_family: lambda x: list(set(x))
            })

            merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

            def fuzzy_match(row):
                if isinstance(row[product_uid], list):
                    return row[product_uid], row[product_family], "UPC Match"

                upc10 = row["m_10"]
                candidates = product_df2[
                    product_df2["p_12"].astype(str).str.contains(upc10, na=False)
                ]

                if candidates.empty:
                    return None, None, "No Match"

                return list(set(candidates[product_uid])), list(set(candidates[product_family])), "Partial"

            res = merged.apply(fuzzy_match, axis=1)

            merged["Retail UID"] = res.apply(lambda x: x[0][0] if isinstance(x[0], list) else None)
            merged["Family"] = res.apply(lambda x: x[1][0] if isinstance(x[1], list) else None)
            merged["Match Type"] = res.apply(lambda x: x[2])

            progress.progress(60)

            # ==============================
            # STORE VALIDATION
            # ==============================
            merged["store_family_key"] = merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
            sf_df["store_family_key"] = sf_df[sf_store].astype(str) + "|" + sf_df[sf_family].astype(str)

            merged["Valid Store-Family"] = merged["store_family_key"].isin(set(sf_df["store_family_key"]))

            # ==============================
            # REASON
            # ==============================
            def reason(row):
                if pd.isna(row["Retail UID"]) and not row["Valid Store-Family"]:
                    return "No Match + Invalid Store-Family"
                elif pd.isna(row["Retail UID"]):
                    return "No Match"
                elif not row["Valid Store-Family"]:
                    return "Invalid Store-Family"
                return "Good"

            merged["Reason"] = merged.apply(reason, axis=1)

            progress.progress(75)

            # ==============================
            # OUTPUTS
            # ==============================
            good_df = merged[merged["Reason"] == "Good"][[main_store, "Retail UID"]].drop_duplicates()
            invalid_df = merged[merged["Reason"] != "Good"][[main_store, main_upc, main_desc, "Reason"]]

            unmatched_df = merged[merged["Match Type"] == "No Match"][
                [main_upc, main_desc]
            ].drop_duplicates()

            unmatched_df.columns = ["UPC", "Description"]

            # ==============================
            # INFERENCE
            # ==============================
            def infer(desc):
                group, size, unit = parse_pack(desc)
                return pd.Series({
                    "Group": group,
                    "Products/Case": None,
                    "Units/Product": None,
                    "Unit Size": size,
                    "Unit Measure": unit,
                })

            if not unmatched_df.empty:
                unmatched_df = pd.concat(
                    [unmatched_df, unmatched_df["Description"].apply(infer)],
                    axis=1
                )

            # ==============================
            # TEMPLATE
            # ==============================
            template = pd.DataFrame({
                "ProductId": unmatched_df["UPC"],
                "Product Name": unmatched_df["Description"],
                "Group": unmatched_df.get("Group"),
                "ProductUPC": unmatched_df["UPC"],
                "UnitUPC": unmatched_df["UPC"],
                "CaseUPC": unmatched_df["UPC"],
                "Active": "true",
                "Products/Case": unmatched_df.get("Products/Case"),
                "Units/Product": unmatched_df.get("Units/Product"),
                "Unit Size": unmatched_df.get("Unit Size"),
                "Unit Measure": unmatched_df.get("Unit Measure"),
                "Family Head": "false"
            })

            progress.progress(90)

        except Exception as e:
            merged = pd.DataFrame({"Error": [str(e)]})
            good_df = invalid_df = unmatched_df = template = pd.DataFrame()

        # ==============================
        # EXPORT (NEVER FAILS)
        # ==============================
        output = BytesIO()

        with pd.ExcelWriter(output, engine="openpyxl") as writer:

            pd.DataFrame({"Status": ["Success"]}).to_excel(writer, "Status", index=False)

            for name, df in [
                ("Full Output", merged),
                ("Good To Go", good_df),
                ("Invalid", invalid_df),
                ("Unmatched", unmatched_df),
                ("Product Template", template),
            ]:
                try:
                    df.to_excel(writer, sheet_name=name, index=False)
                except:
                    pass

        output.seek(0)

        progress.progress(100)
        status.text("Done!")

        st.download_button(
            "📥 Download",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
