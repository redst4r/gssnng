import anndata
import numpy as np
from scipy import sparse
import pandas as pd
import scanpy as sc
#import tqdm
from anndata import AnnData
from gssnng.smoothing import nn_smoothing
from gssnng.util import error_checking
from gssnng.score_funs import scorefun
from gssnng.gene_sets import genesets
from typing import Union
from multiprocessing import Pool

def with_gene_sets(
        adata: anndata.AnnData,
        gene_set_file: str,
        groupby: Union[str,dict],
        recompute_neighbors: int,
        score_method: str,
        method_params: dict,
        samp_neighbors: int,
        noise_trials: int,
        ranked: bool,
        cores: int
    ) -> anndata.AnnData:

    """
    gene set scoring (all gene sets in file) with nearest neighbor smoothing of the expression matrix

    Improved single cell scoring by:
    - smoothing the data matrix
        - adding noise to the nearest neighbor smoothing via `samp_neighbors`
    - adding noise to the expression data itself (via noise_trials)

    :param adata: anndata.AnnData containing the cells to be scored
    :param gene_set_file: the gene set file with list of gene sets, gmt, one per line
    :param groupby: either a column label in adata.obs, and all categories taken, or a dict specifies one group.
    :param recompute_neighbors: should neighbors be recomputed within each group, 0 for no, >0 for yes and specifies N
    :param score_method: which scoring method to use
    :param method_params: specific params for each method.
    :param samp_neighbors: number of neighbors to sample
    :param noise_trials: number of noisy samples to create after sampling, integer
    :param ranked: whether the gene expression counts should be rank ordered
    :param cores: number of parallel processes to work through groupby groups

    :returns: adata with gene set scores in .obs
    """

    # our gene set data object list
    gs_obj = genesets(gene_set_file)

    if error_checking(adata, samp_neighbors, gs_obj, score_method, ranked) == 'ERROR':
        return("ERROR")

    # score each cell with the list of gene sets
    all_scores = _proc_data(adata, gs_obj, groupby, recompute_neighbors,
                                  score_method, method_params, samp_neighbors,
                                  noise_trials, ranked, cores)
    ## join in new results
    adata.obs = adata.obs.join(all_scores, how='left')

    print("**done**")
    return(adata)


def _smooth_out(adata, samp_neighbors):
    # NOTE: this is cells x genes
    smoothed_matrix = nn_smoothing(adata.X, adata, 'connectivity', samp_neighbors)
    # for easier handling with gene names
    smoothed_adata = AnnData(smoothed_matrix, obs=adata.obs, var=adata.var)
    return(smoothed_adata)


def _proc_data(
        adata: anndata.AnnData,
        gs_obj: genesets,
        groupby: Union[str,dict],
        recompute_neighbors: int,
        score_method: str,
        method_params: dict,
        samp_neighbors: int,
        noise_trials: int,
        ranked: bool,
        cores: int
                     ):
    """
    In many cases, the neighbors should be defined.  If you have mixed clinical endpoints,
    you might want scores for only the early points together, and scores for later points.
    Or T-cell neighbors should maybe only be other T-cells. etc. Or by leiden cluster label.
    By building out the list of groups, it can also be run in parallel.

    :param adata: anndata.AnnData containing the cells to be scored
    :param gs_obj: the gene sets class object
    :param groupby: either a column label in adata.obs, and all categories taken, or a dict specifies one group.
    :param recompute_neighbors: should the neighbors be recomputed within each group?  SLOW SLOW SLOW
    :param score_method: which scoring method to use
    :param method_params: specific params for each method.
    :param samp_neighbors: number of neighbors to sample
    :param noise_trials: number of noisy samples to create, integer
    :param ranked: whether the gene expression counts should be rank ordered
    :param cores: number of parallel processes to work through groupby groups

    :returns: scores in a dict for each cell in a list.
    """
    data_list = []  # list of dicts

    if groupby is None:  # take all cells
        if recompute_neighbors > 0:
            sc.pp.neighbors(adata, n_neighbors=recompute_neighbors)
        smoothed_adata =  _smooth_out(adata, samp_neighbors)  # list of 1 item
        params = (smoothed_adata, gs_obj, score_method, method_params, noise_trials, ranked, 'single_group')
        data_list.append(params)

    elif type(groupby) == type("string"):
        cats = set(adata.obs[groupby])
        for ci in cats:
            ### then for each group
            qi = adata[adata.obs[groupby] == ci]
            if recompute_neighbors > 0:
                sc.pp.neighbors(qi, n_neighbors=recompute_neighbors)
            ### smooth and make an adata, and add to the list
            qi_smoothed = _smooth_out(qi, samp_neighbors)
            params = (qi_smoothed, gs_obj, score_method, method_params, noise_trials, ranked, ci)
            data_list.append( params )
            ### send off for scores

    elif type(groupby) == type(['a','b']):  # if it's a type list
        return("ERROR")

    elif type(groupby) == {'a':1, 'b':2}:  # if it's a type dict
        return("ERROR: dict not implemented")

    with Pool(processes=cores) as pool:
        res0 = pool.starmap_async(_score_all_cells_all_sets, data_list).get()

    res1 = pd.concat(res0, axis=0)
    return(res1)


def _get_cell_data(
        smoothed_adata: anndata.AnnData,
        cell_ix: int,
        noise_trials: int,
        method_params: dict,
        ranked: bool
) -> pd.DataFrame:
    """
    the processed expression data for each cell

    :param smoothed_adata: anndata.AnnData containing the cells to be scored
    :param cell_ix: index of the cell in adata
    :param noise_trials: number of noisy samples to create, integer
    :param method_params: specific params for each method.

    :return: a data frame
    """
    # for each cell, rank the expression
    gene_mat = smoothed_adata.X[cell_ix]
    # then we subset it to only the genes with counts
    _, gdx, _ = sparse.find(gene_mat)

    if gene_mat.ndim == 2:
        df = pd.DataFrame(gene_mat[:, gdx].A.flatten(), index=smoothed_adata.var.index[gdx])  ## ????
    else:
        df = pd.DataFrame(gene_mat[gdx],
                          index=smoothed_adata.var.index[gdx])  ## not sure why it's coming off as an array
    df.columns = ['counts']

    if ('normalization' in method_params) and (method_params['normalization'] == 'average') and (noise_trials > 0):
        # add some noise to gene counts.. create a n numbers of examples
        raise ValueError('not implemented')
        #df_noise = si.add_noise(df, noise_trials, 0.01, 0.99)  ## slow part .. fixed
    else:
        df_noise = df

    if ranked:
        # for right now always ranking genes up #
        df_noise['uprank'] = df_noise.iloc[:,0].rank(method='min', ascending=True)  # up or down
        df_noise['dnrank'] = np.max(df['uprank']) - df['uprank']

    return(df_noise)


def _score_all_cells_all_sets(
        smoothed_adata: AnnData,
        gene_set_obj: genesets,
        score_method: str,
        method_params: dict,
        noise_trials: int,
        ranked: bool,
        group_name: str
        ) -> pd.DataFrame:
    """
    Process cells and score each with a list of gene sets and a method

    :param smoothed_adata: anndata.AnnData containing the cells to be scored
    :param gene_set_obj: the gene sets class object
    :param score_method: which scoring method to use
    :param method_params: specific params for each method.
    :param noise_trials: number of noisy samples to create, integer
    :param ranked: whether the gene expression counts should be rank ordered
    :param group_name: group of cells currently being processed

    :return: list of list of gene set score dictionaries
    """
    print("running " + group_name)

    results_list = []
    barcodes = []
    for cell_ix in range(smoothed_adata.shape[0]):  # tqdm.trange(smoothed_adata.shape[0]):  # for each cell ID
        results = dict()
        df_cell = _get_cell_data(smoothed_adata, cell_ix, noise_trials, method_params, ranked)
        barcodes.append(smoothed_adata.obs.index[cell_ix])
        for gs_i in gene_set_obj.set_list:  #   for each gene set
            results[gs_i.name] = scorefun(gs_i, df_cell, score_method, method_params, ranked)
        results_list.append(results)
    results_df = pd.DataFrame(results_list, index=barcodes)
    return(results_df)
