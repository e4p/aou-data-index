import connexion
import errors
import requests
from data_index.models.dataset import Dataset
from data_index.models.list_datasets_response import ListDatasetsResponse
from datetime import date, datetime
from typing import List, Dict
from six import iteritems
from data_index import elastic
from data_index.models.dataset import Dataset
from data_index.models.list_datasets_response import ListDatasetsResponse
from data_index.util import deserialize_date, deserialize_datetime
from werkzeug.exceptions import BadRequest, Conflict, InternalServerError, NotFound, NotImplemented


def split_ds_name(ds_name):
    """Takes a full dataset id string and returns the unique ID.

    >>> split_ds_name('datasets/abc123')
    'abc123'

    Args:
      ds_name: (string) full resource name of a dataset, e.g. 'datasets/abc123'

    Returns:
      (string) the dataset resource id, e.g. 'abc123'
    """
    parts = ds_name.split('/')
    if len(parts) != 2 or parts[0] != 'datasets':
        raise BadRequest('malformed dataset name "{}"'.format(ds_name))
    return parts[1]


def doc_to_dataset(doc):
    """Converts an ElasticSearch document dict to a Dataset model.

    Args:
      doc: (dict) an ElasticSearch document representation of a dataset

    Returns:
      (data_index.models.dataset.Dataset) a dataset model instance
    """
    return Dataset(name='datasets/' + doc['_id'])


def dataset_to_doc(dataset):
    """Converts a Dataset model to an ElasticSearch document dict.

    Args:
      dataset: (data_index.models.dataset.Dataset) a dataset model instance

    Returns:
      (dict) an ElasticSearch document representation of the dataset
    """
    # Note: a dataset document is currently empty, future extensions to the
    # dataset API resource will be stored here.
    return {}


def list_datasets(pageSize=64, pageToken=None):
    """
    Paginated method for listing all datasets in the repository.

    Args:
      pageSize: The int maximum number of datasets to return per page. The
        server may return fewer. If unspecified, defaults to 64. Maximum value
        is 1024.
      pageToken: The string continuation token, which is used to page through
        large result sets. To get the next page of results, set this parameter
        to the value of nextPageToken from the previous response.

    Returns:
      a dictionary representation of a ListDatasetsResponse
    """
    raise NotImplemented()


def create_dataset(body):
    """
    Create a dataset.

    Args:
      body: the dataset JSON

    Returns:
      the newly created Dataset
    """
    if not connexion.request.is_json:
        raise BadRequest('got unexpected non-JSON payload')
    dataset = Dataset.from_dict(body)
    if not dataset or not dataset.name:
        raise BadRequest('missing required dataset.name from request')

    ds_id = split_ds_name(dataset.name)

    # Each dataset has two associated indices, which together form a
    # bidirectional mapping of individual <-> data pointer using Elastic's
    # parent-child document semantics. Allow these indices to exist already, in
    # the event that a previous create_dataset() was partially successful.
    def raise_for_index_create_status(r):
        if r.status_code != requests.codes.ok and (
                elastic.error_type(r.json()) != elastic.INDEX_ALREADY_EXISTS):
            r.raise_for_status()

    r = requests.put(
        elastic.by_ppl_index(ds_id),
        json={
            'mappings': {
                'individuals': {},
                'data': {
                    '_parent': {
                        'type': 'individuals',
                    },
                },
            },
        },
        headers=elastic.REQ_HEADERS)
    raise_for_index_create_status(r)
    r = requests.put(
        elastic.by_data_index(ds_id),
        json={
            'mappings': {
                'data': {},
                'individuals': {
                    '_parent': {
                        'type': 'data',
                    },
                },
            },
        },
        headers=elastic.REQ_HEADERS)
    raise_for_index_create_status(r)

    # Create the dataset metadata doc last to mark successful creation.
    r = requests.put(
        elastic.ds_index_path(ds_id),
        params={
            'op_type': 'create',
            # Wait for the document to be indexed. Dataset operations are low
            # throughput, so this performance tradeoff is acceptable.
            'refresh': 'true',
        },
        json=dataset_to_doc(dataset),
        headers=elastic.REQ_HEADERS)
    if r.status_code == requests.codes.conflict:
        raise Conflict('dataset "{}" already exists'.format(dataset.name))
    r.raise_for_status()

    return get_dataset(ds_id)


def get_dataset(datasetId):
    """
    Gets a dataset.

    Args:
      datasetId: the dataset to retrieve

    Returns:
      the requested Dataset
    """
    r = requests.get(elastic.ds_index_path(datasetId))
    if r.status_code == requests.codes.not_found:
        raise errors.DatasetNotFound(datasetId)
    r.raise_for_status()
    return doc_to_dataset(r.json())


def update_dataset(datasetId, body):
    """
    Updates a dataset.

    Args:
      datasetId: the dataset to update
      body: JSON representation of the updated Dataset

    Returns:
      the updated Dataset
    """
    if not connexion.request.is_json:
        raise InternalServerError('got unexpected non-JSON payload')
    dataset = Dataset.from_dict(body)
    if dataset.name:
        ds_id = split_ds_name(dataset.name)
        if ds_id != datasetId:
            raise BadRequest(
                'URL dataset name "datasets/{}" and dataset.name "{}" must agree'.
                format(datasetId, dataset.name))
    else:
        dataset.name = 'datasets/' + datasetId

    # Raises an error if the dataset is not found.
    get_dataset(ds_id)

    # For now, we only accept full replacement updates.
    requests.put(
        elastic.ds_index_path(ds_id),
        params={
            # Wait for the document to be indexed. Dataset operations are low
            # throughput, so this performance tradeoff is acceptable.
            'refresh': 'true',
        },
        json=dataset_to_doc(dataset),
        headers=elastic.REQ_HEADERS).raise_for_status()

    return get_dataset(ds_id)


def delete_dataset(datasetId):
    """
    Deletes a dataset.

    Args:
      datasetId: the dataset to delete
    """
    responses = []
    # We cannot apply these deletions in a transaction, so we must tolerate
    # failures between any of the following mutations. Delete the dataset
    # metadata document first to indicate to the rest of the system that the
    # dataset is no longer active.
    for path in [
            elastic.ds_index_path(datasetId),
            elastic.by_ppl_index(datasetId),
            elastic.by_data_index(datasetId)
    ]:
        r = requests.delete(path)
        responses.append(r)

    # This method should be retryable, even if it fails half-way through. To
    # that end, only return a 404 if we've previously deleted all associated
    # indices for the dataset.
    if all([r.status_code == requests.codes.not_found for r in responses]):
        raise errors.DatasetNotFound(datasetId)

    # If a deletion failed for any other reason, raise an internal error.
    for r in responses:
        if r.status_code != requests.codes.not_found:
            # Only raises an exception for non-2XXs.
            r.raise_for_status()
