def maximum_weight_assignment(weights: list[list[int]]) -> list[tuple[int, int]]:
    """직사각 가중치 행렬의 최대 합 1:1 매칭을 Hungarian algorithm으로 구한다."""

    if not weights or not weights[0]:
        return []
    row_count = len(weights)
    column_count = len(weights[0])
    if any(len(row) != column_count for row in weights):
        raise ValueError("All assignment weight rows must have the same length.")

    size = max(row_count, column_count)
    max_weight = max(max(row) for row in weights)
    costs = [
        [
            max_weight - (weights[row][column] if row < row_count and column < column_count else 0)
            for column in range(size)
        ]
        for row in range(size)
    ]
    assignment = _minimum_cost_assignment(costs)
    return [
        (row, column)
        for row, column in enumerate(assignment[:row_count])
        if column < column_count and weights[row][column] > 0
    ]


def _minimum_cost_assignment(costs: list[list[int]]) -> list[int]:
    size = len(costs)
    row_potential = [0] * (size + 1)
    column_potential = [0] * (size + 1)
    matched_row_by_column = [0] * (size + 1)
    previous_column = [0] * (size + 1)

    for row in range(1, size + 1):
        matched_row_by_column[0] = row
        minimum_reduced_cost = [10**18] * (size + 1)
        used = [False] * (size + 1)
        current_column = 0
        while True:
            used[current_column] = True
            current_row = matched_row_by_column[current_column]
            delta = 10**18
            next_column = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                reduced_cost = (
                    costs[current_row - 1][column - 1]
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if reduced_cost < minimum_reduced_cost[column]:
                    minimum_reduced_cost[column] = reduced_cost
                    previous_column[column] = current_column
                if minimum_reduced_cost[column] < delta:
                    delta = minimum_reduced_cost[column]
                    next_column = column
            for column in range(size + 1):
                if used[column]:
                    row_potential[matched_row_by_column[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum_reduced_cost[column] -= delta
            current_column = next_column
            if matched_row_by_column[current_column] == 0:
                break
        while True:
            prior = previous_column[current_column]
            matched_row_by_column[current_column] = matched_row_by_column[prior]
            current_column = prior
            if current_column == 0:
                break

    assignment = [-1] * size
    for column in range(1, size + 1):
        if matched_row_by_column[column] != 0:
            assignment[matched_row_by_column[column] - 1] = column - 1
    return assignment
