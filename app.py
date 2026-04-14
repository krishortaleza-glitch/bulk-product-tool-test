import streamlit as st
import pandas as pd
from datetime import datetime
from rapidfuzz import fuzz
import re
import tempfile

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

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

def clean_upc(series):
    return (
        series.astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\D", "", regex=True)
    )

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
        variants.add("0" + upc)
    if len(upc) == 10:
        variants.add("0" + upc)
    if len(upc) < 12:
        variants.add(upc.zfill(12))

    return variants

def infer_family_smart(desc, product_df, product_desc_col, family_col):
    try:
        desc_clean = desc.lower()
        best = None
        best_score = 0

        for _, row in product_df.iterrows():
            score = fuzz.token_set_ratio(desc_clean, str(row[product_desc_col]).lower())
            if score > best_score:
                best_score = score
                best = row

        if best is None:
            return "", ""

        fam = best[family_col]

        best_type = ""
        if "Type" in product_df.columns:
            subset = product_df[product_df[family_col] == fam]
            if not subset.empty:
                best_type = subset["Type"].mode().iloc[0]

        return fam, best_type
    except:
        return "", ""

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
    main_store = st.selectbox("Store Number", main_df.columns)

    if st.button("🚀 Process Files"):

        try:
            progress = st.progress(0)
            status = st.empty()

            status.text("Cleaning...")
            main_df["desc_clean"] = clean_desc(main_df[main_desc])
            product_df["desc_clean"] = clean_desc(product_df[product_desc])

            # Build UPC table
            rows = []
            for _, r in product_df.iterrows():
                if pd.notna(r[product_upc1]):
                    row = r.copy()
                    row["UPC_list"] = r[product_upc1]
                    row["UPC_SOURCE"] = "ProductUPC"
                    rows.append(row)

                if pd.notna(r[product_upc2]):
                    row = r.copy()
                    row["UPC_list"] = r[product_upc2]
                    row["UPC_SOURCE"] = "UnitUPC"
                    rows.append(row)

            product_df = pd.DataFrame(rows)
            progress.progress(20)

            # UPC MATCH
            status.text("Matching UPCs...")
            product_lookup = {}

            for _, row in product_df.iterrows():
                upc = clean_upc(pd.Series([row["UPC_list"]])).iloc[0]
                for v in normalize_upc_variants(upc):
                    product_lookup.setdefault(v, []).append(row)

            def match_upc(row):
                upc = clean_upc(pd.Series([row[main_upc]])).iloc[0]

                matches = []
                sources = set()

                for v in normalize_upc_variants(upc):
                    if v in product_lookup:
                        for m in product_lookup[v]:
                            matches.append(m)
                            sources.add(m["UPC_SOURCE"])

                if not matches and len(upc) == 10:
                    contains = product_df[
                        product_df["UPC_list"].astype(str).str.contains(upc, na=False)
                    ]
                    if not contains.empty:
                        matches = contains.to_dict("records")
                        for m in matches:
                            sources.add(f"Contains({m['UPC_SOURCE']})")

                if not matches:
                    return None, None, "No Match"

                uids = sorted(set([m[product_uid] for m in matches]))
                families = list(set([m[product_family] for m in matches]))

                source_label = list(sources)[0] if len(sources) == 1 else "Mixed"

                return uids, families, source_label

            upc_results = main_df.apply(match_upc, axis=1)

            merged = main_df.copy()
            merged["All Retail UIDs"] = upc_results.apply(lambda x: x[0])
            merged["All Families"] = upc_results.apply(lambda x: x[1])
            merged["Match Source"] = upc_results.apply(lambda x: x[2])

            progress.progress(50)

            # DESCRIPTION MATCH
            status.text("Matching descriptions...")

            def fuzzy_match(row):
                if isinstance(row["All Retail UIDs"], list):
                    return row["All Retail UIDs"], row["All Families"], 100, "UPC Match", row["Match Source"]

                desc = row["desc_clean"]
                exact = product_df[product_df["desc_clean"] == desc]

                if not exact.empty:
                    return (
                        list(set(exact[product_uid])),
                        list(set(exact[product_family])),
                        100,
                        "Exact Description Match",
                        "Description Match"
                    )

                return None, None, 0, "No Match", "No Match"

            results = merged.apply(fuzzy_match, axis=1)

            merged["All Retail UIDs"] = results.apply(lambda x: x[0])
            merged["All Families"] = results.apply(lambda x: x[1])
            merged["Match Score"] = results.apply(lambda x: x[2])
            merged["Match Type"] = results.apply(lambda x: x[3])
            merged["Match Source"] = results.apply(lambda x: x[4])

            progress.progress(75)

            # STORE FAMILY VALIDATION
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

            # OUTPUT
            status.text("Building output...")

            summary = merged["Match Type"].value_counts().reset_index()
            summary.columns = ["Match Type", "Count"]

            good_df = merged[
                (merged["Retail UID"].notna()) &
                (merged["Valid Store-Family"])
            ][[main_store, "Retail UID"]].drop_duplicates()
            good_df.columns = ["Store", "Retail UID"]

            invalid_df = merged[
                (merged["Retail UID"].isna()) |
                (~merged["Valid Store-Family"])
            ][[main_store, main_upc, main_desc]]
            invalid_df.columns = ["Store", "UPC", "Description"]

            unmatched_df = merged[merged["Match Type"] == "No Match"][
                [main_upc, main_desc]
            ].drop_duplicates()
            unmatched_df.columns = ["UPC", "Description"]

            invalid_sf_df = merged[~merged["Valid Store-Family"]][
                [main_store, "Family"]
            ].drop_duplicates()
            invalid_sf_df.columns = ["Store", "Family"]

            # FAMILY INFERENCE
            results = unmatched_df["Description"].apply(
                lambda x: infer_family_smart(x, product_df, product_desc, product_family)
            )

            unmatched_df["Family"] = results.apply(lambda x: x[0])
            unmatched_df["Type"] = results.apply(lambda x: x[1])

            # TEMPLATE
            template_df = pd.DataFrame({
                "ProductId": unmatched_df["UPC"],
                "Product Name": unmatched_df["Description"],
                "Type": unmatched_df["Type"],
                "Family": unmatched_df["Family"],
                "ProductUPC": unmatched_df["UPC"],
                "Active": "true"
            })

            # MULTIPLE MATCHES (FULL DEDUPE)
            multi_match_df = merged[
                merged["All Retail UIDs"].apply(lambda x: isinstance(x, list) and len(x) > 1)
            ][[main_upc, main_desc, "All Retail UIDs"]].copy()

            multi_match_df.columns = ["UPC", "Description", "Retail UIDs"]

            multi_match_df["Retail UIDs"] = multi_match_df["Retail UIDs"].apply(lambda x: sorted(set(x)))

            # REMOVE DUPLICATE ROWS (critical fix)
            multi_match_df = multi_match_df.drop_duplicates(subset=["UPC", "Description"])

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
                merged.to_excel(writer, sheet_name="Full Output", index=False)
                summary.to_excel(writer, sheet_name="Summary", index=False)
                good_df.to_excel(writer, sheet_name="Good To Go", index=False)
                invalid_df.to_excel(writer, sheet_name="Invalid For Portal", index=False)
                unmatched_df.to_excel(writer, sheet_name="Unmatched Products", index=False)
                invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)
                multi_match_df.to_excel(writer, sheet_name="Multiple Matches", index=False)
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

        except Exception as e:
            st.error(f"Critical error: {e}")
