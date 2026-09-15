"""すべての境界契約で共有する厳格な値型。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
SignedInt = Annotated[int, Field(strict=True)]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]
InputText = Annotated[str, Field(min_length=1, max_length=8000)]
NarrationText = Annotated[str, Field(min_length=1, max_length=12000)]
Ref = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Contract(BaseModel):
    """未知フィールドと生成後の変更を拒否する基底契約。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
