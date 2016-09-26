import time, datetime, types, copy
import importlib

from pymongo.errors import DuplicateKeyError, BulkWriteError

import biothings.dataload.uploader as uploader
from biothings.utils.mongo import get_src_conn, get_src_dump
from biothings.utils.common import get_timestamp, get_random_string, timesofar, dump2gridfs, iter_n
from biothings.utils.dataload import list2dict, merge_struct


__sources_dict__ = {
    'entrez': [
        'entrez.entrez_gene',
        'entrez.entrez_homologene',
        'entrez.entrez_genesummary',
        'entrez.entrez_accession',
        'entrez.entrez_refseq',
        'entrez.entrez_unigene',
        'entrez.entrez_go',
        'entrez.entrez_ec',
        'entrez.entrez_retired',
        'entrez.entrez_generif',
        'entrez.entrez_genomic_pos',
    ],
    'ensembl': [
        'ensembl.ensembl_gene',
        'ensembl.ensembl_acc',
        'ensembl.ensembl_genomic_pos',
        'ensembl.ensembl_prosite',
        'ensembl.ensembl_interpro',
        'ensembl.ensembl_pfam'
    ],
    'uniprot': [
        'uniprot',
        'uniprot.uniprot_pdb',
        # 'uniprot.uniprot_ipi',   # IPI is now discontinued, last update is still in the db, but won't be updated.
        'uniprot.uniprot_pir'
    ],
    'pharmgkb': ['pharmgkb'],
    'reporter': ['reporter'],
    'ucsc': ['ucsc.ucsc_exons'],
    'exac': ['exac.broadinstitute_exac'],
    'cpdb': ['cpdb'],
    'reagent': ['reagent'],
}


class MyGeneSourceUploader(uploader.SourceStorage):

    def add_custom_source_metadata(self,metadata):
        if metadata.get('ENTREZ_GENEDOC_ROOT', False):
            metadata['get_geneid_d'] = src_m.get_geneid_d
        if metadata.get('ENSEMBL_GENEDOC_ROOT', False):
            metadata['get_mapping_to_entrez'] = src_m.get_mapping_to_entrez

class GeneDocSource(uploader.DefaultSourceUploader):

    def post_update_data(self):
        t0 = time.time()
        if getattr(self, 'ENTREZ_GENEDOC_ROOT', False):
            print('Uploading "geneid_d" to GridFS...', end='')
            geneid_d = self.get_geneid_d()
            dump2gridfs(geneid_d, self.__collection__ + '__geneid_d.pyobj', self.db)
        if getattr(self, 'ENSEMBL_GENEDOC_ROOT', False):
            print('Uploading "mapping2entrezgene" to GridFS...', end='')
            x2entrezgene_list = self.get_mapping_to_entrez()
            dump2gridfs(x2entrezgene_list, self.__collection__ + '__2entrezgene_list.pyobj', self.db)
        print('Done[%s]' % timesofar(t0))

    def doc_iterator(self, doc_d, batch=True, step=10000):
        if isinstance(doc_d, types.GeneratorType) and batch:
            for doc_li in iter_n(doc_d, n=step):
                yield doc_li
        else:
            if batch:
                doc_li = []
                i = 0
            for _id, doc in doc_d.items():
                doc['_id'] = _id
                _doc = copy.copy(self)
                _doc.clear()
                _doc.update(doc)
                #if validate:
                #    _doc.validate()
                if batch:
                    doc_li.append(_doc)
                    i += 1

                    if i % step == 0:
                        yield doc_li
                        doc_li = []
                else:
                    yield _doc

            if batch:
                yield doc_li

    def update_data(self, doc_d, step):
        doc_d = doc_d or self.load_data()
        print("Uploading to the DB...", end='')
        t0 = time.time()
        tinner = time.time()
        aslistofdict = None
        for doc_li in self.doc_iterator(doc_d, batch=True, step=step):
            toinsert = len(doc_li)
            nbinsert = 0
            print("Inserting %s records ... " % toinsert,end="", flush=True)
            try:
                bob = self.temp_collection.initialize_unordered_bulk_op()
                for d in doc_li:
                    aslistofdict = d.pop("__aslistofdict__",None)
                    bob.insert(d)
                res = bob.execute()
                nbinsert += res["nInserted"]
                print("OK [%s]" % timesofar(tinner))
            except BulkWriteError as e:
                inserted = e.details["nInserted"]
                nbinsert += inserted
                print("Fixing %d records " % len(e.details["writeErrors"]),end="",flush=True)
                ids = [d["op"]["_id"] for d in e.details["writeErrors"]]
                # build hash of existing docs
                docs = self.temp_collection.find({"_id" : {"$in" : ids}})
                hdocs = {}
                for doc in docs:
                    hdocs[doc["_id"]] = doc
                bob2 = self.temp_collection.initialize_unordered_bulk_op()
                for err in e.details["writeErrors"]:
                    errdoc = err["op"]
                    existing = hdocs[errdoc["_id"]]
                    assert "_id" in existing
                    _id = errdoc.pop("_id")
                    merged = merge_struct(errdoc, existing,aslistofdict=aslistofdict)
                    bob2.find({"_id" : _id}).update_one({"$set" : merged})
                    # update previously fetched doc. if several errors are about the same doc id,
                    # we would't merged things properly without an updated document
                    assert "_id" in merged
                    hdocs[_id] = merged
                    nbinsert += 1

                res = bob2.execute()
                print("OK [%s]" % timesofar(tinner))
            assert nbinsert == toinsert, "nb %s to %s" % (nbinsert,toinsert)
            # end of loop so it counts the time spent in doc_iterator
            tinner = time.time()

        print('Done[%s]' % timesofar(t0))
        self.switch_collection()
        self.post_update_data()

    def generate_doc_src_master(self):
        _doc = {"_id": str(self.__collection__),
                "name": str(self.__collection__),
                "timestamp": datetime.datetime.now()}
        for attr in ['ENTREZ_GENEDOC_ROOT', 'ENSEMBL_GENEDOC_ROOT', 'id_type']:
            if hasattr(self, attr):
                _doc[attr] = getattr(self, attr)
        if hasattr(self, 'get_mapping'):
            _doc['mapping'] = getattr(self, 'get_mapping')()

        return _doc

