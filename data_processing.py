import pandas as pd
import pickle
import torch
import torchani
import torchani_mod
import qcelemental as qcel
import numpy as np
from tqdm import tqdm
from rdkit import Chem
from biopandas.pdb import PandasPdb


def elements_to_atomicnums(elements):
    atomicnums = np.zeros(len(elements), dtype=int)

    for idx, e in enumerate(elements):
        atomicnums[idx] = qcel.periodictable.to_Z(e)

    return atomicnums


def LoadMolasDF(mol):
    m = mol

    atoms = []

    for atom in m.GetAtoms():
        if atom.GetSymbol() != "H": # Include only non-hydrogen atoms
            entry = [int(atom.GetIdx())]
            entry.append(str(atom.GetSymbol()))
            pos = m.GetConformer().GetAtomPosition(atom.GetIdx())
            entry.append(float("{0:.4f}".format(pos.x)))
            entry.append(float("{0:.4f}".format(pos.y)))
            entry.append(float("{0:.4f}".format(pos.z)))
            atoms.append(entry)

    df = pd.DataFrame(atoms)
    df.columns = ["ATOM_INDEX","ATOM_TYPE","X","Y","Z"]
    
    return df


def LoadPDBasDF_old(PDB, atom_keys):
    prot_atoms = []
    
    f = open(PDB)
    for i in f:
        if i[:4] == "ATOM":
            # Include only non-hydrogen atoms
            if (len(i[12:16].replace(" ","")) < 4 and i[12:16].replace(" ","")[0] != "H") or (len(i[12:16].replace(" ","")) == 4 and i[12:16].replace(" ","")[1] != "H" and i[12:16].replace(" ","")[0] != "H"):
                prot_atoms.append([int(i[6:11]),
                         i[17:20]+"-"+i[12:16].replace(" ",""),
                         float(i[30:38]),
                         float(i[38:46]),
                         float(i[46:54])
                        ])
                
    f.close()
    
    df = pd.DataFrame(prot_atoms, columns=["ATOM_INDEX","PDB_ATOM","X","Y","Z"])
    df = df.merge(atom_keys, left_on='PDB_ATOM', right_on='PDB_ATOM')[["ATOM_INDEX", "ATOM_TYPE", "X", "Y", "Z"]].sort_values(by="ATOM_INDEX").reset_index(drop=True)
    if list(df["ATOM_TYPE"].isna()).count(True) > 0:
        print("WARNING: Protein contains unsupported atom types. Only supported atom-type pairs are counted.")
    return df


def LoadPDBasDF(pdb_path, atom_keys):
    allowed_residues = atom_keys["RESIDUE"].unique()
    ppdb = PandasPdb().read_pdb(pdb_path)
    protein = ppdb.df['ATOM']
    protein = protein[~protein["atom_name"].str.startswith("H")]
    protein = protein[~protein["atom_name"].str.startswith(tuple(map(str, range(10))))]

    disgard = protein[~protein["residue_name"].isin(allowed_residues)]
    if len(disgard) > 0:
        print("WARNING: Protein contains unsupported residues.", pdb_path)
        print("Ignoring following residues:")
        print(disgard["residue_name"].unique())

    protein = protein[protein["residue_name"].isin(allowed_residues)]
    protein["PDB_ATOM"] = protein["residue_name"] + "-" + protein["atom_name"]
    protein = protein[['atom_number','PDB_ATOM','x_coord','y_coord','z_coord']].rename(columns={"atom_number":"ATOM_INDEX", "x_coord":"X", "y_coord":"Y", "z_coord":"Z"})
    protein = protein.merge(atom_keys, how='left', on='PDB_ATOM').sort_values(by="ATOM_INDEX").reset_index(drop=True)

    if list(protein["ATOM_TYPE"].isna()).count(True) > 0:
        print("WARNING: Protein contains unsupported atom types.", pdb_path)
        print("Ignoring following atom types:")
        print(protein[protein["ATOM_TYPE"].isna()]["PDB_ATOM"].unique())
    return protein


def GetMolAEVs_extended(protein_path, mol, atom_keys, radial_coefs, atom_map):
    # Protein and ligand structure are loaded as pandas DataFrame
    Target = LoadPDBasDF(protein_path, atom_keys)
    Ligand = LoadMolasDF(mol)
    
    # Define AEV coeficients
    # Radial coefficients
    RcR = radial_coefs[0]
    EtaR = radial_coefs[1]
    RsR = radial_coefs[2]
    
    # Angular coefficients (Ga)
    RcA = 2.0
    Zeta = torch.tensor([1.0])
    TsA = torch.tensor([1.0]) # Angular shift in GA
    EtaA = torch.tensor([1.0])
    RsA = torch.tensor([1.0])  # Radial shift in GA
    
    # Reduce size of Target df to what we need based on radial cutoff RcR
    distance_cutoff = RcR + 0.1
    
    for i in ["X","Y","Z"]:
        Target = Target[Target[i] < float(Ligand[i].max())+distance_cutoff]
        Target = Target[Target[i] > float(Ligand[i].min())-distance_cutoff]
    
    Target = Target.merge(atom_map, on='ATOM_TYPE', how='left')
    
    # Create tensors of atomic numbers and coordinates of molecule atoms and 
    # protein atoms combined. Encode molecule atoms as hydrogen
    mol_len = torch.tensor(len(Ligand))
    atomicnums = np.append(np.ones(mol_len)*6, Target["ATOM_NR"])
    atomicnums = torch.tensor(atomicnums, dtype=torch.int64)
    atomicnums = atomicnums.unsqueeze(0)
    
    coordinates = pd.concat([Ligand[['X','Y','Z']], Target[['X','Y','Z']]])
    coordinates = torch.tensor(coordinates.values)
    coordinates = coordinates.unsqueeze(0)
    
    # Use torchani_mod to calculate AEVs
    atom_symbols = []
    for i in range(1, 23):
        atom_symbols.append(qcel.periodictable.to_symbol(i))
        
    AEVC = torchani_mod.AEVComputer(RcR, RcA, EtaR, RsR, 
                                EtaA, Zeta, RsA, TsA, len(atom_symbols))
    
    SC = torchani.SpeciesConverter(atom_symbols)
    sc = SC((atomicnums, coordinates))
    
    aev = AEVC.forward((sc.species, sc.coordinates), mol_len)
    
    # find indices of columns to keep
    # keep all radial terms
    n = len(atom_symbols)
    n_rad_sub = len(EtaR)*len(RsR)
    indices = list(np.arange(n*n_rad_sub))
    
    return Ligand, aev.aevs.squeeze(0)[:mol_len,indices]


def one_of_k_encoding(x, allowable_set):
    if x not in allowable_set:
        raise Exception("input {0} not in allowable set{1}:".format(x, allowable_set))
    return list(map(lambda s: x == s, allowable_set))


def atom_features(atom, features=["atom_symbol",
                                  "num_heavy_atoms", 
                                  "total_num_Hs", 
                                  "explicit_valence", 
                                  "is_aromatic", 
                                  "is_in_ring"]):
    # Computes the ligand atom features for graph node construction
    # The standard features are the following:
    # atom_symbol = one hot encoding of atom symbol
    # num_heavy_atoms = # of heavy atom neighbors
    # total_num_Hs = # number of hydrogen atom neighbors
    # explicit_valence = explicit valence of the atom
    # is_aromatic = boolean 1 - aromatic, 0 - not aromatic
    # is_in_ring = boolean 1 - is in ring, 0 - is not in ring

    feature_list = []
    if "atom_symbol" in features:
        feature_list.extend(one_of_k_encoding(atom.GetSymbol(),['F', 'N', 'Cl', 'O', 'Br', 'C', 'B', 'P', 'I', 'S']))
    if "num_heavy_atoms" in features:
        feature_list.append(len([x.GetSymbol() for x in atom.GetNeighbors() if x.GetSymbol() != "H"]))
    if "total_num_Hs" in features:
        feature_list.append(len([x.GetSymbol() for x in atom.GetNeighbors() if x.GetSymbol() == "H"]))
    if "explicit_valence" in features: #-NEW ADDITION FOR PLIG
        feature_list.append(atom.GetExplicitValence())
    if "is_aromatic" in features:
        
        if atom.GetIsAromatic():
            feature_list.append(1)
        else:
            feature_list.append(0)
    if "is_in_ring" in features:
        if atom.IsInRing():
            feature_list.append(1)
        else:
            feature_list.append(0)
    
    return np.array(feature_list)


def mol_to_graph(mol, mol_df, aevs, extra_features=["atom_symbol",
                                                    "num_heavy_atoms", 
                                                    "total_num_Hs", 
                                                    "explicit_valence",
                                                    "is_aromatic",
                                                    "is_in_ring"]):

    features = []
    heavy_atom_index = []
    idx_to_idx = {}
    counter = 0
    
    # Generate nodes
    for atom in mol.GetAtoms():
        if atom.GetSymbol() != "H": # Include only non-hydrogen atoms
            idx_to_idx[atom.GetIdx()] = counter
            aev_idx = mol_df[mol_df['ATOM_INDEX'] == atom.GetIdx()].index
            heavy_atom_index.append(atom.GetIdx())
            feature = np.append(atom_features(atom), aevs[aev_idx,:])
            features.append(feature)
            counter += 1
    
    #Generate edges
    edges = []
    for bond in mol.GetBonds():
        idx1 = bond.GetBeginAtomIdx()
        idx2 = bond.GetEndAtomIdx()
        if idx1 in heavy_atom_index and idx2 in heavy_atom_index:
            bond_type = one_of_k_encoding(bond.GetBondType(),[1,12,2,3])
            bond_type = [float(b) for b in bond_type]
            edge1 = [idx_to_idx[idx1], idx_to_idx[idx2]]
            edge1.extend(bond_type)
            edge2 = [idx_to_idx[idx2], idx_to_idx[idx1]]
            edge2.extend(bond_type)
            edges.append(edge1)
            edges.append(edge2)
    
    df = pd.DataFrame(edges, columns=['atom1', 'atom2', 'single', 'aromatic', 'double', 'triple'])
    df = df.sort_values(by=['atom1','atom2'])
    
    edge_index = df[['atom1','atom2']].to_numpy().tolist()
    edge_attr = df[['single','aromatic','double','triple']].to_numpy().tolist()
    
    
    return len(mol_df), features, edge_index, edge_attr



"""
Load data
"""

df = pd.read_csv("data/dataset.csv", index_col=0)

"""
Check what molecules are readible by RDKit, and which contain rare atoms
"""
allowed_elements = set(['F', 'N', 'Cl', 'O', 'Br', 'C', 'B', 'P', 'I', 'S'])
non_readable = []
rare_atoms_ids = []
for index, row in tqdm(df.iterrows()):
    suppl = Chem.SDMolSupplier(row["sdf_file"], removeHs=False)
    assert(len(suppl) == 1)
    lig = suppl[0]
    if lig is None:
        non_readable.append(row["unique_id"])
    else:
        mol_df = LoadMolasDF(lig)
        if not set(mol_df["ATOM_TYPE"].values).issubset(allowed_elements):
            rare_atoms_ids.append(row["unique_id"])

print("Number of sdf files not read by RDkit:",len(non_readable))
print("Number of sdf files with rare elements:",len(rare_atoms_ids))
df = df[~df["unique_id"].isin(rare_atoms_ids)].reset_index(drop=True)
df = df[~df["unique_id"].isin(non_readable)].reset_index(drop=True)


"""
Check what protein structures are readible by Biopandas
"""
atom_keys = pd.read_csv("data/PDB_Atom_Keys.csv", sep=",")
atom_keys["RESIDUE"] = atom_keys["PDB_ATOM"].apply(lambda x: x.split("-")[0])
non_readable = []
for index, row in tqdm(df.iterrows()):
    try:
        LoadPDBasDF(row["pdb_file"], atom_keys)
    except:
        non_readable.append(row["unique_id"])

print("Number of pdb files not read by Biopandas:",len(non_readable))
df = df[~df["unique_id"].isin(non_readable)].reset_index(drop=True)


"""
Analyse atom features
"""
features = []
for index, row in tqdm(df.iterrows()):
    suppl = Chem.SDMolSupplier(row["sdf_file"], removeHs=False)
    lig = suppl[0]
    for atom in lig.GetAtoms():
        if atom.GetSymbol() != "H":
            feature = []
            feature.append(atom.GetSymbol())
            feature.append(len([x.GetSymbol() for x in atom.GetNeighbors() if x.GetSymbol() != "H"]))
            feature.append(len([x.GetSymbol() for x in atom.GetNeighbors() if x.GetSymbol() == "H"]))
            feature.append(atom.GetExplicitValence())                
            if atom.GetIsAromatic():
                feature.append(1)
            else:
                feature.append(0)
            if atom.IsInRing():
                feature.append(1)
            else:
                feature.append(0)
        
        features.append(feature)

features = pd.DataFrame(features, columns=["atom_symbol",
                                            "num_heavy_atoms",
                                            "total_num_Hs",
                                            "explicit_valence",
                                            "is_aromatic",
                                            "is_in_ring"])

print(features["atom_symbol"].value_counts())
print(features["num_heavy_atoms"].value_counts())
print(features["total_num_Hs"].value_counts())
print(features["explicit_valence"].value_counts())
print(features["is_aromatic"].value_counts())
print(features["is_in_ring"].value_counts())


"""
Edge analysis
"""
bond_types = []
unspecified_bond_mol = []
for index, row in tqdm(df.iterrows()):
    suppl = Chem.SDMolSupplier(row["sdf_file"], removeHs=False)
    lig = suppl[0]

    heavy_atom_index = []
    idx_to_idx = {}
    counter = 0
    for atom in lig.GetAtoms():
        if atom.GetSymbol() != "H": # Include only non-hydrogen atoms
            idx_to_idx[atom.GetIdx()] = counter
            heavy_atom_index.append(atom.GetIdx())
            counter += 1
    
    conf = lig.GetConformer()
    for bond in lig.GetBonds():
        idx1 = bond.GetBeginAtomIdx()
        idx2 = bond.GetEndAtomIdx()
        if idx1 in heavy_atom_index and idx2 in heavy_atom_index:
            bond_types.append(bond.GetBondType())
            if bond.GetBondType() == 0:
                unspecified_bond_mol.append(row["unique_id"])

features = pd.DataFrame(data={"bond_type":bond_types})
print(features["bond_type"].value_counts())

print("Number of molecules with unspecified bond types:",len(unspecified_bond_mol))
df = df[~df["unique_id"].isin(unspecified_bond_mol)].reset_index(drop=True)

# save the processed dataset
df.to_csv("data/dataset_processed.csv")


"""
Generate for all complexes: ANI-2x with 22 atom types. Only 2-atom interactions.
"""
#df = pd.read_csv("data/dataset_processed.csv", index_col=0)
#atom_keys = pd.read_csv("data/PDB_Atom_Keys.csv", sep=",")

print("Generating graphs\n")

atom_map = pd.DataFrame(pd.unique(atom_keys["ATOM_TYPE"]))
atom_map[1] = list(np.arange(len(atom_map)) + 1)
atom_map = atom_map.rename(columns={0:"ATOM_TYPE", 1:"ATOM_NR"})

# Radial coefficients: ANI-2x
RcR = 5.1 # Radial cutoff
EtaR = torch.tensor([19.7]) # Radial decay
RsR = torch.tensor([0.80, 1.07, 1.34, 1.61, 1.88, 2.14, 2.41, 2.68, 
                    2.95, 3.22, 3.49, 3.76, 4.03, 4.29, 4.56, 4.83]) # Radial shift
radial_coefs = [RcR, EtaR, RsR]

mol_graphs = {}
for index, row in tqdm(df.iterrows()):
    suppl = Chem.SDMolSupplier(row["sdf_file"], removeHs=False)
    lig = suppl[0]

    protein_path = row["pdb_file"]

    mol_df, aevs = GetMolAEVs_extended(protein_path, lig, atom_keys, radial_coefs, atom_map)
    graph = mol_to_graph(lig, mol_df, aevs)
    mol_graphs[row["unique_id"]] = graph

#save the graphs to use as input for the GNN models
output_file_graphs = "data/graphs.pickle"
with open(output_file_graphs, 'wb') as handle:
    pickle.dump(mol_graphs, handle, protocol=pickle.HIGHEST_PROTOCOL)
