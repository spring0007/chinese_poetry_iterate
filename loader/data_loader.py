import json
import os


_HERE = os.path.dirname(os.path.abspath(__file__))
DATAS_CONFIG = os.path.join(_HERE, "datas.json")


def _as_records(data):
    """Normalise a parsed dataset file into a list of records.

    ``datas.json`` declares each dataset's top-level shape as ``root``:
    ``"list"`` for a JSON array of records (every dataset written before
    ``root`` existed), ``"dict"`` for a single bare record, and
    ``"dict_or_list"`` when the files of one dataset disagree. ``四书五经/``
    is the ``dict_or_list`` case: ``daxue.json`` and ``zhongyong.json`` are
    bare ``{chapter, paragraphs}`` dicts while ``mengzi.json`` is an array of
    records with that same shape.

    All three are accepted here regardless of what is declared. This loader
    predates the declaration and used to iterate the parsed file directly, so
    treating a dict as a single record cannot change the result for any
    dataset that existed before. The declaration itself is enforced by
    ``loader.mapping``, which does fail loudly on an unexpected shape.
    """
    return data if isinstance(data, list) else [data]


class PlainDataLoader():
    def __init__(self, config_path: str=DATAS_CONFIG) -> None:
        self._path = config_path
        with open(config_path, 'r', encoding='utf-8') as config:
            data = json.load(config)
            # Paths in datas.json are written relative to the repository root,
            # which is the directory above the loader/ holding the config.
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
            self.top_level_path:str = os.path.normpath(
                os.path.join(repo_root, data["cp_path"])
            )
            self.datasets:dict = data["datasets"]
            self.id_table = {
                v["id"]: k for (k, v) in self.datasets.items()
            }
    
    def body_extractor(self, target: str) -> list:
        if target not in self.datasets:
            print(f"{target} is not included in datas.json as a dataset")
            return None
        configs = self.datasets[target]
        tag = configs["tag"]
        body = []  # may get a bit huge... 
        full_path = os.path.join(self.top_level_path, configs["path"])
        if os.path.isfile(full_path):  # single file json
            with open(full_path, mode='r', encoding='utf-8') as file:
                data = json.load(file)
                for poem in _as_records(data):
                    body += poem[tag]
            return body
        # a dir, probably with a skip list
        excludes = set(configs.get("excludes", []))
        subpaths = sorted(os.listdir(full_path))
        for filename in subpaths:
            if filename in excludes:
                continue
            with open(os.path.join(full_path, filename), mode='r', encoding='utf-8') as file:
                data = json.load(file)
                for poem in _as_records(data):
                    body += poem[tag]
        return body

    def extract_from_multiple(self, targets: list) -> list:
        results = []
        for target in targets:
            results += self.body_extractor(target)
        return results
    
    def extract_with_ids(self, ids: list) -> list:
        results = []
        for id in ids:
            results += self.body_extractor(
                self.id_table[id]
            )
        return results



if __name__ == "__main__":
    loader = PlainDataLoader()
    print(loader.id_table)
    # print(
    #     loader.body_extractor("wudai-huajianji")[-1]
    # )
    # print(
    #     len(loader.extract_from_multiple(["wudai-huajianji", "wudai-nantang"]))
    # )
    print(
        loader.extract_with_ids([2])
    )

