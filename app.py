import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
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
# FAMILY DETECTION (NEW)
# ==============================
def extract_family(desc, product_df):
    desc = str(desc).lower()

    families = product_df["Family"].dropna().unique()

    for fam in families:
        if str(fam).lower() in desc:
            return fam

    return ""

# ==============================
# PACK PARSER
# ==============================
def parse_pack(desc):
    desc = str(desc).lower()

    # SINGLE
    m = re.search(r"1\s*/\s*([\d\.]+)", desc)
    if m:
        size = float(m.group(1))
        if size == 19:
            size = 19.2
        return "Singles", size, "OZ", True

    # MULTI
    m = re.search(r"(\d+)\s*/\s*([\d\.]+)", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = float(m.group(2))
        if size == 19:
            size = 19.2
        return group, size, "OZ", False

    return "", "", "", False

def safe_mode(df, col):
    if col not in df.columns or df.empty:
        return ""
    if df[col].mode().empty:
        return ""
    return df[col].mode().iloc[0]

# ==============================
# UI
# ==============================
adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store File", type=["xlsx"])

if adm_file and product_file and store_file:

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    product_df.columns = product_df.columns.str.strip()

    # COLUMN SELECTION
    main_upc = st.selectbox("Main UPC", main_df.columns)
    main_desc = st.selectbox("Main Description", main_df.columns)
    main_store = st.selectbox("Main Store", main_df.columns)

    product_upc1 = st.selectbox("Product UPC 1", product_df.columns)
    product_upc2 = st.selectbox("Product UPC 2", product_df.columns)
    product_desc = st.selectbox("Product Description", product_df.columns)
    product_uid = st.selectbox("Product UID", product_df.columns)
    product_family = st.selectbox("Product Family", product_df.columns)

    sf_store = st.selectbox("Store Column", sf_df.columns)
    sf_family = st.selectbox("Family Column", sf_df.columns)

    if st.button("🚀 Process Files"):

        try:
            # CLEAN
            main_df["desc_clean"] = clean_desc(main_df[main_desc])
            product_df["desc_clean"] = clean_desc(product_df[product_desc])

            generate_keys(main_df, main_upc, "m")

            product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
            product_df = product_df.explode("UPC_list")
            generate_keys(product_df, "UPC_list", "p")

            # ==============================
            # EXACT MATCH
            # ==============================
            map_12 = product_df.groupby("p_12").agg({
                product_uid: lambda x: list(set(x)),
                product_family: lambda x: list(set(x))
            })

            merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

            merged["Retail UID"] = merged[product_uid].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["Family"] = merged[product_family].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["Match Type"] = merged["Retail UID"].apply(
                lambda x: "UPC Match" if pd.notna(x) else "No Match"
            )

            # ==============================
            # STORE VALIDATION
            # ==============================
            merged["store_family_key"] = merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
            sf_df["store_family_key"] = sf_df[sf_store].astype(str) + "|" + sf_df[sf_family].astype(str)

            merged["Valid Store-Family"] = merged["store_family_key"].isin(set(sf_df["store_family_key"]))

            # ==============================
            # UNMATCHED
            # ==============================
            unmatched_df = merged[merged["Match Type"] == "No Match"][
                [main_upc, main_desc]
            ].drop_duplicates()

            unmatched_df.columns = ["UPC", "Description"]

            # PARSE PACK
            parsed = unmatched_df["Description"].apply(parse_pack)

            unmatched_df["Group"] = parsed.apply(lambda x: x[0])
            unmatched_df["Unit Size"] = parsed.apply(lambda x: x[1])
            unmatched_df["Unit Measure"] = parsed.apply(lambda x: x[2])
            unmatched_df["Is Single"] = parsed.apply(lambda x: x[3])

            # ==============================
            # FAMILY INFERENCE (NEW)
            # ==============================
            unmatched_df["Family"] = unmatched_df["Description"].apply(
                lambda x: extract_family(x, product_df)
            )

            # ==============================
            # INFERENCE ENGINE
            # ==============================
            def infer(row):

                # SINGLE
                if row["Is Single"]:
                    candidates = product_df[
                        product_df["Group"].str.contains("single", case=False, na=False)
                    ]

                    return pd.Series({
                        "Products/Case": safe_mode(candidates, "Products/Case"),
                        "Units/Product": 1,
                        "Unit Size": row["Unit Size"],
                        "Unit Measure": row["Unit Measure"],
                        "Match Type": "Single Match"
                    })

                # GROUP MATCH
                candidates = product_df[
                    product_df["Group"].str.contains(str(row["Group"]), na=False)
                ]

                if not candidates.empty:
                    return pd.Series({
                        "Products/Case": safe_mode(candidates, "Products/Case"),
                        "Units/Product": safe_mode(candidates, "Units/Product"),
                        "Unit Size": row["Unit Size"],
                        "Unit Measure": row["Unit Measure"],
                        "Match Type": "Group Match"
                    })

                # FAMILY + SIZE MATCH (NEW 🔥)
                candidates = product_df[
                    (product_df["Family"] == row["Family"]) &
                    (product_df["Unit Size"].astype(str) == str(row["Unit Size"]))
                ]

                if not candidates.empty:
                    return pd.Series({
                        "Products/Case": safe_mode(candidates, "Products/Case"),
                        "Units/Product": safe_mode(candidates, "Units/Product"),
                        "Unit Size": safe_mode(candidates, "Unit Size"),
                        "Unit Measure": safe_mode(candidates, "Unit Measure"),
                        "Match Type": "Family Size Match"
                    })

                return pd.Series({
                    "Products/Case": "",
                    "Units/Product": "",
                    "Unit Size": row["Unit Size"],
                    "Unit Measure": row["Unit Measure"],
                    "Match Type": "No Match"
                })

            inferred = unmatched_df.apply(infer, axis=1)

            unmatched_df["Products/Case"] = inferred["Products/Case"]
            unmatched_df["Units/Product"] = inferred["Units/Product"]
            unmatched_df["Match Type"] = inferred["Match Type"]

            # ==============================
            # OUTPUT TABS
            # ==============================
            good_df = merged[
                (merged["Retail UID"].notna()) &
                (merged["Valid Store-Family"])
            ]

            invalid_df = merged[
                (merged["Retail UID"].isna()) |
                (~merged["Valid Store-Family"])
            ]

            invalid_sf_df = merged[
                ~merged["Valid Store-Family"]
            ]

            # ==============================
            # TEMPLATE
            # ==============================
            template_df = pd.DataFrame({
                "ProductId": unmatched_df["UPC"],
                "UnitId": "",
                "CaseId": "",
                "Product Name": unmatched_df["Description"],
                "Type": "",
                "Family": unmatched_df["Family"],
                "Group": unmatched_df["Group"],
                "ProductUPC": unmatched_df["UPC"],
                "UnitUPC": "",
                "CaseUPC": "",
                "Active": "true",
                "Products/Case": unmatched_df["Products/Case"],
                "Units/Product": unmatched_df["Units/Product"],
                "Unit Size": unmatched_df["Unit Size"],
                "Unit Measure": unmatched_df["Unit Measure"],
                "ParentId": "",
                "Family Head": "false"
            })

            # ==============================
            # EXPORT
            # ==============================
            output = BytesIO()
            writer = pd.ExcelWriter(output, engine="openpyxl")

            pd.DataFrame({"Status": ["OK"]}).to_excel(writer, sheet_name="Status", index=False)
            merged.to_excel(writer, sheet_name="Full Output", index=False)
            good_df.to_excel(writer, sheet_name="Good to Go", index=False)
            invalid_df.to_excel(writer, sheet_name="Invalid for Portal", index=False)
            invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched", index=False)
            template_df.to_excel(writer, sheet_name="Product Template", index=False)

            writer.close()
            output.seek(0)

            st.success("✅ Done")

            st.download_button(
                "📥 Download",
                data=output,
                file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
            )

        except Exception as e:
            st.error(f"❌ Error: {e}")
