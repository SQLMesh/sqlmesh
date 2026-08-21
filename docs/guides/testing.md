# Testing guide
## Testing changes to models
To run unit tests for your models, run the `sqlmesh test` command as follows:

```bash
$ sqlmesh test
.
----------------------------------------------------------------------
Ran 1 test in 0.042s

OK
```
As the unit tests run, SQLMesh will identify any that fail.

By default, `sqlmesh plan` runs unit tests only for models included in the plan (added, modified, or restated). Plans with no such models skip unit tests. Use `--all-tests` to run the full suite, or `--skip-tests` to run none.

For more information about tests, refer to [testing](../concepts/tests.md).

### Test changes to a specific model

To run a specific model test, pass in the suite file name followed by `::` and the name of the test; for example: `sqlmesh test tests/test_suite.yaml::test_example_full_model`.

### Run a subset of tests

To run a test that matches a pattern or substring, use the following syntax: `sqlmesh test tests/test_example*`.

Running the above command will run our `test_example_full_model` test that we ran earlier using `sqlmesh test`:

```
$ sqlmesh test tests/test_example*
.
----------------------------------------------------------------------
Ran 1 test in 0.042s

OK
```

As another example, running the `sqlmesh test tests/test_order*` command would run the following tests:

* `test_orders`
* `test_orders_takeout`
* `test_order_items`
* `test_order_type`

## Auditing changes to models
To audit your models, run the `sqlmesh audit` command as follows:

```bash
$ sqlmesh audit
Found 1 audit(s).
assert_positive_order_ids PASS.

Finished with 0 audit error(s).
Done.
```
**Note:** Ensure that you have already planned and applied your changes before running an audit.

By default, SQLMesh will halt the pipeline when an audit fails in order to prevent potentially invalid data from propagating further downstream. This behavior can be changed for individual audits by defining them as [non-blocking](../concepts/audits.md#non-blocking-audits).

For more information about audits, refer to [auditing](../concepts/audits.md).
