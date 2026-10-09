"""A model whose name collides with the other module's, for the duplicate-binding fence.

`SClient` in two modules is what forces both to be aliased; the service name
then makes one alias land on the generated client class.
"""

from pydantic import BaseModel


class SClient(BaseModel):
    n: int = 1
